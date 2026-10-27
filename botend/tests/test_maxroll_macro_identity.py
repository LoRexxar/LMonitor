"""Real source -> isolated ORM -> translate_blocks; never call a supplier."""
import copy
import json
from pathlib import Path

from django.test import TestCase

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion
from botend.services.article_translation_service import ArticleTranslationService
from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_service import translate_blocks
from botend.services.wow_news_glossary_service import WowNewsGlossary


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/maxroll_pet_macro_identity.json').read_text())


def section(key, refs, code='/cast Axe Toss'):
    return {'id': key, 'type': 'details', 'children': [
        {'id': key + '-ref', 'type': 'html', 'html': ' '.join(f'[[spell:{sid}]]' for sid in refs)},
        {'id': key + '-code', 'type': 'code', 'data': {'code': code}},
    ]}


class MaxrollMacroIdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'major_version': '12.1', 'branch': 'retail', 'is_active': True,
            'current_build': '12.1.5.70575',
        })
        WowSpellSnapshot.objects.bulk_create([WowSpellSnapshot(**row) for row in FIXTURE['snapshots']])
        WowTalentNodeMetadata.all_objects.bulk_create([
            WowTalentNodeMetadata(talent_version=cls.version, localization_only=True,
                name_kind=row['name_kind'], reference_id=row['reference_id'],
                name=row['name'], name_zh=row['name_zh']) for row in FIXTURE['supplemental']
        ])
        # Neither a phrase nor an unscoped macro override can supply identity.
        for kind in ('phrase', 'macro'):
            WowTalentNodeMetadata.all_objects.create(talent_version=cls.version,
                localization_only=True, name_kind=kind, reference_id=999,
                name='Axe Toss', name_zh='错误覆盖')

    def translate(self, blocks, version='12.1.5'):
        original = copy.deepcopy(blocks)
        service = ArticleTranslationService(engine=False, glossary=WowNewsGlossary.from_trusted_pairs([]))
        result, failed = translate_blocks(blocks, version, service=service)
        self.assertEqual(blocks, original)
        return {b['id']: b for b in walk_blocks(result)}, [r for r in failed if r['field'] == 'code']

    def test_real_guide123_pet_section_uses_central_spellname_not_supplemental_homonym(self):
        WowTalentNodeMetadata.all_objects.filter(name_kind__in=['phrase', 'macro']).delete()
        before = list(WowSpellSnapshot.objects.order_by('pk').values())
        terms = list(WowTalentNodeMetadata.all_objects.order_by('pk').values())
        blocks, failed = self.translate([FIXTURE['pet_section']])
        expected_names = {'Axe Toss': '巨斧投掷', 'Command Demon': '恶魔掌控',
                          'Singe Magic': '烧灼驱魔', 'Spell Lock': '法术封锁',
                          'Shadow Bulwark': '暗影壁垒', 'Grimoire of Sacrifice': '牺牲魔典'}
        replay = []
        for source in walk_blocks([FIXTURE['pet_section']]):
            if source['type'] != 'code':
                continue
            expected = source['data']['code']
            for english, chinese in expected_names.items():
                expected = expected.replace(english, chinese)
            self.assertEqual(blocks[source['id']]['data']['code'], expected)
            replay.append({'id': source['id'], 'code': expected})
        axe_blocks = [b for b in replay if '巨斧投掷' in b['code']]
        self.assertEqual([b['id'] for b in axe_blocks], ['b-123-2-4', 'b-123-2-8'])
        self.assertEqual({r['id']: {d['operand'] for d in r['operands']} for r in failed}, {
            'b-123-2-4': {'Suffering', 'Seduce'}, 'b-123-2-8': {'Suffering', 'Seduce'},
            'b-123-2-12': {'Devour Magic', 'Felstorm'},
            'b-123-2-16': {'Devour Magic', 'Lesser Invisibility'},
        })
        self.assertEqual(list(WowSpellSnapshot.objects.order_by('pk').values()), before)
        self.assertEqual(list(WowTalentNodeMetadata.all_objects.order_by('pk').values()), terms)
        print('REAL_SOURCE_REPLAY=' + json.dumps({'source': FIXTURE['provenance'],
            'outputs': replay, 'unresolved': failed}, ensure_ascii=False))

    def test_sections_do_not_share_identity_and_equal_chinese_is_not_unique_identity(self):
        blocks, failed = self.translate([
            section('pet', [119898]), section('boss', [474062]),
            section('conflict', [89766, 474062]), section('same-zh', [89766, 119914]),
            section('absent', []),
        ])
        self.assertEqual(blocks['pet-code']['data']['code'], '/cast 巨斧投掷')
        self.assertEqual(blocks['boss-code']['data']['code'], '/cast 利斧投掷')
        self.assertEqual({r['id']: r['operands'][0]['status'] for r in failed}, {
            'conflict-code': 'ambiguous_identity', 'same-zh-code': 'ambiguous_identity',
            'absent-code': 'missing_identity'})
        for key in ('conflict', 'same-zh', 'absent'):
            self.assertEqual(blocks[key + '-code']['data']['code'], '/cast Axe Toss')
        same = next(r for r in failed if r['id'] == 'same-zh-code')['operands'][0]
        self.assertEqual(same['candidate_ids'], [89766, 119914])
        absent = next(r for r in failed if r['id'] == 'absent-code')['operands'][0]
        self.assertEqual(len(absent['unscoped_candidates']), 6)

    def test_missing_chinese_dependency_branch_and_build_fail_closed(self):
        for change, status in [('chinese', 'missing_zh'), ('dependency', 'incomplete_context'),
                               ('branch', 'incomplete_context'), ('build', 'incomplete_context'),
                               ('cycle', 'incomplete_context')]:
            with self.subTest(change=change):
                sid = 89766 if change == 'chinese' else 89808
                row = WowSpellSnapshot.objects.get(branch='wow', locale='zhCN', spell_id=sid)
                original = {k: getattr(row, k) for k in ('name_zh', 'branch', 'snapshot_build', 'description')}
                if change == 'chinese':
                    row.name_zh = ''
                elif change in ('dependency', 'branch'):
                    row.branch = 'wowxptr'
                elif change == 'build':
                    row.snapshot_build = '12.1.0.99999'
                else:
                    row.description = '$@spellname119898'
                row.save()
                try:
                    blocks, failed = self.translate([section('pet', [119898])])
                    self.assertEqual(blocks['pet-code']['data']['code'], '/cast Axe Toss')
                    self.assertEqual(failed[0]['operands'][0]['status'], status)
                finally:
                    WowSpellSnapshot.objects.filter(pk=row.pk).update(**original)

    def test_spelldesc_is_not_a_castable_alias(self):
        WowSpellSnapshot.objects.filter(branch='wow', locale='zhCN', spell_id=119914).update(name='Wrapper')
        blocks, failed = self.translate([section('wrapper', [119914])])
        self.assertEqual(blocks['wrapper-code']['data']['code'], '/cast Axe Toss')
        self.assertEqual(failed[0]['operands'][0]['status'], 'missing_identity')

    def test_direct_code_reference_has_priority_and_later_context_does_not_leak(self):
        own = section('own', [119914])
        own['children'][1]['html'] = '[[spell:89766]]'
        flat = [
            {'id': 'h1', 'type': 'heading', 'html': '[[spell:89766]]'},
            {'id': 'first', 'type': 'code', 'data': {'code': '/cast Axe Toss'}},
            {'id': 'h2', 'type': 'heading', 'html': '其他小节'},
            {'id': 'before', 'type': 'code', 'data': {'code': '/cast Axe Toss'}},
            {'id': 'later', 'type': 'html', 'html': '[[spell:474062]]'},
            {'id': 'after', 'type': 'code', 'data': {'code': '/cast Axe Toss'}},
            {'id': 'not-adjacent', 'type': 'code', 'data': {'code': '/cast Axe Toss'}},
        ]
        blocks, failed = self.translate([own] + flat)
        self.assertEqual(blocks['own-code']['data']['code'], '/cast 巨斧投掷')
        self.assertEqual(blocks['first']['data']['code'], '/cast 巨斧投掷')
        self.assertEqual(blocks['after']['data']['code'], '/cast 利斧投掷')
        self.assertEqual({row['id'] for row in failed}, {'before', 'not-adjacent'})

    def test_dependency_budget_rejects_instead_of_truncating_and_no_persistent_cache(self):
        root = WowSpellSnapshot.objects.get(branch='wow', locale='zhCN', spell_id=119898)
        original = root.description
        root.description += ''.join(f'$@spellname{sid} ' for sid in range(900000, 900257))
        root.save()
        blocks, failed = self.translate([section('budget', [119898])])
        self.assertEqual(blocks['budget-code']['data']['code'], '/cast Axe Toss')
        decision = failed[0]['operands'][0]
        self.assertEqual(decision['status'], 'incomplete_context')
        self.assertEqual(decision['issues'][0]['reason'], 'dependency_budget')
        self.assertTrue(set(range(900000, 900257)) <= set(decision['issues'][0]['spell_ids']))
        root.description = original
        root.save()
        blocks, failed = self.translate([section('budget', [119898])])
        self.assertEqual(blocks['budget-code']['data']['code'], '/cast 巨斧投掷')
        self.assertEqual(failed, [])

    def test_unscoped_or_stale_ad_hoc_binding_cannot_change_branch_or_identity(self):
        # No persistent binding contract is introduced. A payload hint cannot
        # stand in for current source tokens or smuggle a stale hash binding.
        block = {'id': 'unbound', 'type': 'code', 'data': {'code': '/cast Axe Toss',
            'macro_bindings': {'Axe Toss': {'spell_id': 89766, 'source_hash': 'stale'}}}}
        blocks, failed = self.translate([block])
        self.assertEqual(blocks['unbound']['data']['code'], '/cast Axe Toss')
        self.assertEqual(failed[0]['operands'][0]['status'], 'missing_identity')
        WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'branch': 'ptr', 'major_version': '12.1.5', 'is_active': True,
        })
        # An explicit ptr request cannot borrow the wow snapshot.
        blocks, failed = self.translate([section('ptr', [89766])], 'ptr')
        self.assertEqual(blocks['ptr-code']['data']['code'], '/cast Axe Toss')
        self.assertEqual(failed[0]['operands'][0]['status'], 'incomplete_context')

    def test_named_item_does_not_become_a_same_name_spell(self):
        block = section('items', [89766], '/equip Axe Toss\n/use Axe Toss\n/use item:89766')
        block['children'][0]['html'] += ' [[item:89766]]'
        blocks, failed = self.translate([block])
        self.assertEqual(blocks['items-code']['data']['code'], block['children'][1]['data']['code'])
        self.assertEqual({r['kind'] for r in failed[0]['operands']}, {'item', 'spell_or_item'})
        self.assertTrue(all(r['status'] == 'incomplete_context' for r in failed[0]['operands']))

    def test_real_repair_fixtures_typed_talent_self_name_and_items(self):
        from botend.models import WowItemSnapshot
        from botend.services.class_guide_macros import macro_block_references, macro_operands, resolve_macro_operands
        from botend.services import wow_localization
        fixture = json.loads((Path(__file__).parent / 'fixtures/maxroll_macro_repair.json').read_text())
        targets = {126: ('b-102-2-2', 'Battle Stance', '战斗姿态'),
                   128: (None, 'Shadowstep', '暗影步'),
                   138: ('b-123-3-6', 'Residual Neural Channeling Agent', '神经传导残剂'),
                   140: ('b-113-3-10', 'Grotesque Vial', '怪诞之瓶')}
        for guide in fixture['guides']:
            gid = int(guide['guide_id'])
            if gid not in targets:
                continue
            with self.subTest(guide=gid):
                WowSpellSnapshot.objects.all().delete()
                WowTalentNodeMetadata.all_objects.all().delete()
                WowItemSnapshot.objects.all().delete()
                WowSpellSnapshot.objects.bulk_create([WowSpellSnapshot(**r) for r in guide['snapshots']])
                for raw in guide['raw_talents']:
                    row = {k: v for k, v in raw.items() if k not in ('id', 'talent_version_id')}
                    WowTalentNodeMetadata.all_objects.create(talent_version=self.version, **row)
                WowItemSnapshot.objects.bulk_create([WowItemSnapshot(**r) for r in fixture['items']])
                before = list(WowSpellSnapshot.objects.order_by('pk').values())
                blocks, failed = self.translate(guide['blocks'])
                bid, english, chinese = targets[gid]
                sources = [b for b in walk_blocks(guide['blocks']) if b['type'] == 'code'
                           and english in b.get('data', {}).get('code', '') and (bid is None or b['id'] == bid)]
                self.assertTrue(sources)
                for source in sources:
                    self.assertIn(chinese, blocks[source['id']]['data']['code'])
                    self.assertNotIn(english, blocks[source['id']]['data']['code'])
                self.assertEqual(list(WowSpellSnapshot.objects.order_by('pk').values()), before)
                if gid == 126:
                    refs = macro_block_references(guide['blocks'])[bid]
                    context = wow_localization.macro_identity_context(refs, self.version)
                    decisions = resolve_macro_operands([('spell', english)], context, refs)
                    decision = decisions[('spell', english.casefold())]
                    self.assertEqual(decision['identity'], ('spell', 386164))
                    self.assertEqual(decision['snapshot_build'], '12.1.0.69814')
                    self.assertTrue(any(e.get('entry_id') == 112184 and e.get('talent_build') == '12.1.5.70575'
                                        for e in decision['relations']))

    def test_full_frozen_macro_replay_preserves_resolved_and_true_ambiguities(self):
        from collections import Counter
        from botend.models import WowItemSnapshot
        from botend.services.class_guide_macros import macro_block_references, macro_operands, resolve_macro_operands
        from botend.services.wow_localization import macro_identity_context
        fixture = json.loads((Path(__file__).parent / 'fixtures/maxroll_macro_repair.json').read_text())
        counts, changes, total = Counter(), [], 0
        self.assertEqual(len(fixture['guides']), 31)
        for guide in fixture['guides']:
            WowSpellSnapshot.objects.all().delete()
            WowTalentNodeMetadata.all_objects.all().delete()
            WowItemSnapshot.objects.all().delete()
            WowSpellSnapshot.objects.bulk_create([WowSpellSnapshot(**r) for r in guide['snapshots']])
            WowItemSnapshot.objects.bulk_create([WowItemSnapshot(**r) for r in fixture['items']])
            for raw in guide['raw_talents']:
                row = {k: v for k, v in raw.items() if k not in ('id', 'talent_version_id')}
                WowTalentNodeMetadata.all_objects.create(talent_version=self.version, **row)
            refs = macro_block_references(guide['blocks'])
            for replay in guide['macro_replay']:
                total += 1
                local = refs[replay['id']]
                self.assertEqual(local, replay['source_refs'])
                context = macro_identity_context(local, self.version)
                decisions = resolve_macro_operands(macro_operands(replay['source_code']), context, local)
                for old in replay['decisions']:
                    new = decisions[(old['kind'], old['operand'].casefold())]
                    counts[new['status']] += 1
                    if old['status'] == 'resolved':
                        if new['status'] == 'resolved':
                            self.assertEqual(list(new['identity']), old['identity'])
                            self.assertEqual(new['name_zh'], old['name_zh'])
                        else:
                            # Newly inspected Entry evidence may be incomplete;
                            # never retain a former success by ignoring it.
                            self.assertEqual(new['status'], 'incomplete_context')
                            self.assertTrue(new['issues'])
                            self.assertTrue(all(i['reason'] == 'unproven_talent_relation' for i in new['issues']))
                    if old['status'] == 'ambiguous_identity':
                        self.assertIn(new['status'], ('ambiguous_identity', 'incomplete_context'))
                        self.assertEqual(new['candidate_ids'], old['candidate_ids'])
                    if old['status'] != new['status']:
                        changes.append([guide['guide_id'], replay['id'], old['operand'], old['status'], new['status']])
        self.assertEqual(total, 381)
        self.assertEqual(sum(counts.values()), 671)
        self.assertGreater(counts['resolved'], 444)
        print('FROZEN_REPAIR_REPLAY=' + json.dumps({'guides': 31, 'blocks': total, 'counts': counts, 'changes': changes}, ensure_ascii=False))

    def test_typed_identity_rejects_conflicts_stale_and_namespace_collisions(self):
        from botend.models import WowItemSnapshot
        from botend.services.class_guide_macros import resolve_macro_operands
        from botend.services.wow_localization import macro_identity_context
        refs = [{'kind': 'talent', 'object_id': 700, 'source_block': 'ref'}]
        row = WowTalentNodeMetadata.all_objects.create(talent_version=self.version,
            node_id=700, talent_id=123, spell_id=89766, display_spell_id=89766,
            name='Axe Toss', name_zh='巨斧投掷', class_name='Warlock', spec_name='Demo')
        def decision():
            context = macro_identity_context(refs, self.version)
            return resolve_macro_operands([('spell', 'Axe Toss')], context, refs)[('spell', 'axe toss')]
        self.assertEqual(decision()['identity'], ('spell', 89766))
        for fields in ({'display_spell_id': 119914}, {'is_current': False}, {'node_id': 701, 'talent_id': 700}):
            original = {key: getattr(row, key) for key in fields}
            WowTalentNodeMetadata.all_objects.filter(pk=row.pk).update(**fields)
            self.assertEqual(decision()['status'], 'incomplete_context')
            WowTalentNodeMetadata.all_objects.filter(pk=row.pk).update(**original)
        WowTalentNodeMetadata.all_objects.filter(pk=row.pk).update(node_id=701, reference_aliases=[700])
        self.assertEqual(decision()['identity'], ('spell', 89766))
        WowTalentNodeMetadata.all_objects.create(talent_version=self.version,
            node_id=700, spell_id=119914, display_spell_id=119914, name='Axe Toss', name_zh='巨斧投掷')
        # Direct Entry wins over an alias; conflicting same-tier facts do not.
        self.assertEqual(decision()['identity'], ('spell', 119914))
        WowTalentNodeMetadata.all_objects.filter(pk=row.pk).update(node_id=700)
        self.assertEqual(decision()['status'], 'incomplete_context')
        WowItemSnapshot.objects.create(item_id=89766, name='Axe Toss', name_zh='物品名')
        block = section('typed', [89766], '/cast Axe Toss\n/equip Axe Toss\n/use Axe Toss\n/use item:89766')
        block['children'][0]['html'] += ' [[item:89766]]'
        blocks, failed = self.translate([block])
        self.assertEqual(blocks['typed-code']['data']['code'], '/cast 巨斧投掷\n/equip 物品名\n/use Axe Toss\n/use item:89766')
        self.assertEqual(failed[0]['operands'][0]['status'], 'ambiguous_identity')
        self.assertEqual(failed[0]['operands'][0]['candidate_identities'], [('item', 89766), ('spell', 89766)])

    def test_exact_normal_macro_and_non_spell_operands_preserve_syntax(self):
        code = ('#showtooltip Axe Toss\n/cast [known:Axe Toss,noknown:Spell Lock] !Axe Toss; Spell Lock\n'
                '/castsequence reset=target/3 Axe Toss, Spell Lock\n/use 13\n'
                '/targetexact Axe Toss\n/whisper Friend Axe Toss\n'
                '/run print("Axe Toss")\n')
        blocks, failed = self.translate([section('normal', [89766, 19647], code)])
        expected = code.split('/targetexact')[0].replace('Axe Toss', '巨斧投掷').replace('Spell Lock', '法术封锁')
        expected += '/targetexact' + code.split('/targetexact')[1]
        self.assertEqual(blocks['normal-code']['data']['code'], expected)
        self.assertEqual(failed, [])
