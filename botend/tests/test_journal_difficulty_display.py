from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace

from django.test import TestCase
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.wow_item_display import load_item_tooltip_metadata
from botend.services.journal_tooltip import cached_tooltip, tooltip
from botend.portal.adventure_journal import build_loot_projection
from botend.services.journal_loot_snapshot import coordinate


class JournalDifficultyDisplayTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='difficulty-test', season_name='Test',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='difficulty-batch')
        self.item = WowItemSnapshot.objects.create(item_id=250243, name_zh='测试装备', quality=3,
            catalog_type='equipment', slot_key='head', description_zh='+999 智力\n装备：旧效果')
        self.request = dict(item_id=self.item.item_id, difficulty_id=1, item_context=1,
            instance_id=1304, difficulty_rule_build='12.1.0.69587', data_branch='retail',
            allow_default_variant=True, default_variant_order='lowest')
        self.legacy = self.variant('legacy', 292, 9, 3, strict=False)
        self.normal = self.variant('normal', 240, 10, 3)
        self.heroic = self.variant('heroic', 260, 20, 4, difficulty=2)

    def variant(self, key, level, stat, quality, strict=True, difficulty=1):
        metadata = {'difficulty_drop': dict(schema=1, difficulty_id=difficulty, item_context=difficulty,
            instance_id=1304, rule_build='12.1.0.69587', quality=quality, provider='wago',
            bonus_ids=[100 + difficulty])} if strict else {}
        if strict:
            metadata['tooltip_source'] = {'provider': 'wowhead',
                'url': f'https://nether.wowhead.com/tooltip/item/{self.item.item_id}?bonus={100 + difficulty}',
                'game_build': ''}
        return WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
            batch_key=self.season.gear_batch_key, variant_key=key, variant_type='drop_equipment',
            item_level=level, bonus_ids=[100 + difficulty], stats_json={'intellect': stat},
            effects_json=[f'装备：产生 {stat} 点效果'], metadata=metadata, data_branch='retail')

    def read(self, **changes):
        return load_item_tooltip_metadata([{**self.request, **changes}])[0]

    def test_exact_difficulty_projects_same_variant_and_keeps_legacy_default(self):
        for difficulty, variant, quality in [(1, self.normal, 3), (2, self.heroic, 4)]:
            result = self.read(difficulty_id=difficulty, item_context=difficulty)
            self.assertEqual((result['variant_id'], result['item_level'], result['quality']),
                (variant.pk, variant.item_level, quality))
            self.assertEqual(result['stats'], variant.stats_json)
            self.assertIn(str(variant.item_level), result['tooltip'])
            self.assertNotIn('999', result['tooltip'])
            shown = cached_tooltip('item', self.item.item_id, difficulty, '12.1.0.69587',
                instance_id=1304, item_context=difficulty, data_branch='retail')
            self.assertEqual((shown['item_level'], shown['quality']), (variant.item_level, quality))
            self.assertNotIn('参考装等', shown['note'])
            self.assertIn('未记录客户端构建', shown['note'])
        legacy = load_item_tooltip_metadata([dict(item_id=self.item.item_id,
            allow_default_variant=True, default_variant_order='lowest')])[0]
        self.assertEqual(legacy['variant_id'], self.legacy.pk)

    def test_journal_variant_cannot_change_ordinary_unique_branch_inference(self):
        WowItemVariantSnapshot.objects.filter(item=self.item).exclude(pk=self.legacy.pk).delete()
        self.legacy.data_branch = 'ptr'
        self.legacy.save(update_fields=['data_branch'])
        request = dict(item_id=self.item.item_id, allow_default_variant=True, default_variant_order='lowest')
        before = load_item_tooltip_metadata([request])[0]
        self.assertEqual(before['variant_id'], self.legacy.pk)
        self.variant('retail-journal-added', 214, 10, 3)
        after = load_item_tooltip_metadata([request])[0]
        self.assertEqual(after['variant_id'], self.legacy.pk)
        self.assertEqual((after['stats'], after['item_level']), (before['stats'], before['item_level']))

    def test_unassociated_or_invalid_facts_fail_closed(self):
        for changes in [dict(difficulty_id=23), dict(item_context=7), dict(instance_id=9),
                        dict(difficulty_rule_build='other'), dict(difficulty_id=True), dict(data_branch='ptr')]:
            result = self.read(**changes)
            self.assertIsNone(result['variant_id'])
            self.assertFalse(result['tooltip_complete'])
            self.assertIsNone(result['item_level'])
        original = deepcopy(self.normal.metadata)
        for field, value in [('provider', 'wowhead'), ('schema', True), ('quality', True), ('bonus_ids', [999])]:
            self.normal.metadata = deepcopy(original)
            self.normal.metadata['difficulty_drop'][field] = value
            self.normal.save(update_fields=['metadata'])
            self.assertIsNone(self.read()['variant_id'])
        with patch('botend.services.journal_tooltip.requests.Session', side_effect=AssertionError('network')):
            result = tooltip('item', self.item.item_id, 1, '12.1.0.69587', instance_id=1304, item_context=1)
        self.assertEqual(result['status'], 'unresolved')
        self.assertFalse(result['complete'])
        self.assertEqual(result['stats'], [])

    def test_strict_branch_cannot_be_spoofed_or_inferred(self):
        self.normal.metadata['data_branch'] = 'ptr'
        self.normal.save(update_fields=['metadata'])
        self.assertIsNone(self.read(data_branch='ptr')['variant_id'])
        self.assertIsNone(self.read(data_branch='retail')['variant_id'])
        self.normal.data_branch = 'ptr'
        self.normal.save(update_fields=['data_branch'])
        self.legacy.delete()
        self.heroic.delete()
        self.assertIsNone(self.read(data_branch='')['variant_id'])
        self.assertEqual(self.read(data_branch='ptr')['variant_id'], self.normal.pk)
        self.assertIsNone(self.read(data_branch='beta')['variant_id'])

    def test_context_zero_is_typed_and_rule_build_is_not_attribute_build(self):
        self.normal.metadata['difficulty_drop']['item_context'] = 0
        self.normal.save(update_fields=['metadata'])
        result = self.read(item_context=0)
        self.assertEqual(result['variant_id'], self.normal.pk)
        self.assertEqual(result['stats_game_build'], '')
        self.assertIsNone(self.read(item_context=False)['variant_id'])

    def test_english_misc_without_variants_is_non_equipment_without_network(self):
        WowItemVariantSnapshot.objects.filter(item=self.item).delete()
        self.item.name_zh = ''
        self.item.name = 'English Toy'
        self.item.catalog_type = 'misc'
        self.item.save()
        with patch('botend.services.journal_tooltip.requests.Session', side_effect=AssertionError('network')):
            shown = tooltip('item', self.item.item_id, 1, '12.1.0.69587',
                instance_id=1304, item_context=1, data_branch='retail')
        self.assertEqual(shown['status'], 'not_equipment')
        self.assertEqual(shown['name'], 'English Toy')
        self.item.refresh_from_db()
        self.assertEqual(self.item.name_zh, '')

    def test_unresolved_quality_is_not_base_quality(self):
        result = self.read(difficulty_id=23)
        self.assertIsNone(result['quality'])
        self.assertIsNone(result['difficulty_quality'])
        self.assertEqual(result['base_quality'], 3)
        shown = cached_tooltip('item', self.item.item_id, 23, '12.1.0.69587',
            instance_id=1304, item_context=23, data_branch='retail')
        self.assertIsNone(shown['quality'])

    def test_verified_ptr_and_beta_source_labels_do_not_need_preview_marker(self):
        for branch, label in [('ptr', 'PTR'), ('beta', 'Beta'), ('retail', '正式服')]:
            self.normal.data_branch = branch
            self.normal.metadata['data_branch'] = branch
            self.normal.save(update_fields=['data_branch', 'metadata'])
            shown = cached_tooltip('item', self.item.item_id, 1, '12.1.0.69587',
                instance_id=1304, item_context=1, data_branch=branch)
            self.assertEqual(shown['status'], 'complete')
            self.assertIn('Wowhead ' + label, shown['note'])
            self.assertIn('未记录客户端构建', shown['note'])

    def test_two_legal_branches_select_only_exact_row_branch(self):
        ptr = self.variant('ptr-normal', 245, 15, 4)
        ptr.data_branch = 'ptr'
        ptr.metadata['data_branch'] = 'ptr'
        ptr.save(update_fields=['data_branch', 'metadata'])
        self.assertEqual(self.read(data_branch='retail')['variant_id'], self.normal.pk)
        self.assertEqual(self.read(data_branch='ptr')['variant_id'], ptr.pk)

    def test_unknown_quality_template_is_neutral_not_gray_or_base_blue(self):
        from django.template.loader import render_to_string
        drop = dict(item_id=self.item.item_id, name='old', slot=1, quality=3, faction='',
            display_season_id=0, condition_id=0, difficulty_ids=[1], icon=0,
            allowable_classes=-1, inventory_type=1, item_class_id=4, item_subclass_id=1)
        owner = SimpleNamespace(journal_id=1, name='Boss', payload={'loot': [drop]})
        release = SimpleNamespace(manifest={'catalog': {'difficulties': [dict(id=1, context=1)]}})
        projection = build_loot_projection(release, 1304, [owner], 1, dict(key='retail', build='other'))
        row = {**projection['loot'][0], 'visible': True}
        self.assertIsNone(row['quality'])
        html = render_to_string('portal/adventure_journal_detail.html', {
            'tab': 'loot', 'loot_catalog': [row], 'loot': [row], 'instance': {'id': 1304},
            'difficulty': 1, 'source': {'build': 'other'},
        })
        self.assertIn('journal-loot-title quality-unknown', html)
        self.assertIn('data-wow-item-tooltip-quality=""', html)
        self.assertNotIn('quality-None', html)
        self.assertNotIn('journal-loot-title quality-3', html)
        self.assertNotIn('journal-loot-title quality-0', html)

    def test_conflicting_matching_variants_are_not_arbitrarily_selected(self):
        self.variant('conflict', 241, 11, 3)
        self.assertIsNone(self.read()['variant_id'])

    def test_projection_threads_catalog_context_and_quality(self):
        drop = dict(item_id=self.item.item_id, name='old', slot=1, quality=3, faction='',
            display_season_id=0, condition_id=0, difficulty_ids=[1, 2], icon=0,
            allowable_classes=-1, inventory_type=1, item_class_id=4, item_subclass_id=1)
        owner = SimpleNamespace(journal_id=1, name='Boss', payload={'loot': [drop]})
        release = SimpleNamespace(manifest={'catalog': {'difficulties': [dict(id=1, context=1), dict(id=2, context=2)]}})
        for difficulty, quality in [(1, 3), (2, 4)]:
            projection = build_loot_projection(release, 1304, [owner], difficulty,
                dict(key='retail', build='12.1.0.69587'))
            self.assertEqual(projection['loot'][0]['quality'], quality)
            self.assertEqual(projection['loot'][0]['details']['quality'], quality)
            from django.template.loader import render_to_string
            row = {**projection['loot'][0], 'visible': True}
            html = render_to_string('portal/adventure_journal_detail.html', {
                'tab': 'loot', 'loot_catalog': [row], 'loot': [row], 'instance': {'id': 1304},
                'difficulty': difficulty, 'source': {'build': '12.1.0.69587'},
            })
            self.assertIn(f'data-wow-item-tooltip-quality="{quality}"', html)
            self.assertIn(f'journal-loot-title quality-{quality}', html)
            self.assertNotIn('正在加载属性', html)
        self.assertEqual(coordinate(1, 1304, 1, {})['schema'], 2)

    def test_endpoint_reads_exact_snapshot_context_without_network(self):
        import json
        from django.test import RequestFactory
        from botend.portal.adventure_journal import PortalAdventureJournalTooltipView
        data = {'instance': {'difficulty_ids': [1, 2], 'source': {'key': 'retail', 'build': '12.1.0.69587'}},
                'references': {'2': {str(self.item.item_id): [{'id': 1, 'name': 'Boss'}]}}}
        index = {'difficulties': [{'id': 2, 'context': 2}]}
        with patch('botend.portal.adventure_journal.journal_files.read_index', return_value=index), \
                patch('botend.portal.adventure_journal.journal_files.read_instance', return_value=data), \
                patch('botend.portal.adventure_journal.journal_files.select_boss', return_value=([], {'id': 1})), \
                patch('botend.services.journal_tooltip.requests.Session', side_effect=AssertionError('network')):
            response = PortalAdventureJournalTooltipView.as_view()(
                RequestFactory().get('/', {'difficulty': 2}), instance_id=1304, kind='item', entry_id=self.item.item_id)
        shown = json.loads(response.content)
        self.assertEqual((shown['item_level'], shown['quality']), (260, 4))

    def test_lazy_javascript_applies_quality_and_stops_on_unresolved(self):
        import subprocess
        from pathlib import Path
        from django.conf import settings
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Node {
  constructor(classes='') { this.dataset={}; this.hidden=false; this.children=[]; this.textContent='';
    this.classList=new Set(classes.split(' ').filter(Boolean));
    this.classList.remove=this.classList.delete.bind(this.classList); }
  querySelectorAll() {return [];}
  replaceChildren(...children) {this.children=children;}
  append(child) {this.children.push(child);}
  addEventListener() {}
}
(async () => {
  for (const data of [{quality:4, complete:true, item_level:260, stats:['+20 智力'], effects:[], lines:['测试装备']},
                     {quality:null, complete:false, status:'unresolved', note:'此难度未知', lines:['测试装备']}]) {
    const stats=new Node(), effects=new Node(), link=new Node(), symbol=new Node('quality-3'), title=new Node('quality-3');
    const row=new Node(); row.dataset={lootId:'250243',detailsLoaded:'false'};
    row.querySelector=s=>({'[data-item-stats]':stats,'[data-item-effects]':effects,
      '.journal-loot-link':link,'.journal-loot-symbol':symbol,'.journal-loot-title':title}[s] || null);
    let count=0;
    const document={body:{dataset:{journalInstance:'1304',journalDifficulty:'2'}},
      querySelector:s=>s==='.journal-loot-table' ? {} : null,
      querySelectorAll:s=>s==='[data-loot-id][data-details-loaded="false"]' && row.dataset.detailsLoaded==='false' ? [row] : [],
      getElementById:()=>null, addEventListener:()=>{}, createElement:()=>new Node()};
    const context={document, sessionStorage:{getItem:()=>null,removeItem:()=>{}},
      fetch:async()=>{count++;return {ok:true,json:async()=>data};}, console};
    vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(count,1);
    const quality = data.quality === null ? 'unknown' : String(data.quality);
    assert(title.classList.has(`quality-${quality}`));
    assert(symbol.classList.has(`quality-${quality}`));
    assert.equal(link.dataset.wowItemTooltipQuality, data.quality === null ? '' : quality);
    if (data.quality === null) assert(!title.classList.has('quality-3'));
    assert.equal(row.dataset.detailsLoaded,data.complete ? 'true' : 'unresolved');
    if (!data.complete) assert.equal(stats.textContent,'此难度装等与属性未知');
  }
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(Path(settings.BASE_DIR) / 'static/portal/js/adventure-journal.js')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
