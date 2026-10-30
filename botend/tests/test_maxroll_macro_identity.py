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
INTRO_ORACLE = json.loads((Path(__file__).parent / 'fixtures/maxroll_macro_intro_oracle.json').read_text())
INTRO_DECISIONS = {(r['guide_id'], r['block'], r['kind'], r['operand'].casefold()): r
                   for r in INTRO_ORACLE['decisions']}



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
            {'id': 'shared', 'type': 'code', 'data': {'code': '/cast Axe Toss'}},
            {'id': 'shared-third', 'type': 'code', 'data': {'code': '/cast Axe Toss'}},
            {'id': 'interruption', 'type': 'html', 'html': '<p>New explanation without identity.</p>'},
            {'id': 'not-adjacent', 'type': 'code', 'data': {'code': '/cast Axe Toss'}},
        ]
        blocks, failed = self.translate([own] + flat)
        self.assertEqual(blocks['own-code']['data']['code'], '/cast 巨斧投掷')
        self.assertEqual(blocks['first']['data']['code'], '/cast 巨斧投掷')
        self.assertEqual(blocks['after']['data']['code'], '/cast 利斧投掷')
        self.assertEqual(blocks['shared']['data']['code'], '/cast 利斧投掷')
        self.assertEqual(blocks['shared-third']['data']['code'], '/cast 利斧投掷')
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
            # Fixed reviewed oracle for this older capture; do not compute
            # expectations with the implementation under test.
            expected_refs = INTRO_ORACLE['legacy_refs'][str(guide['guide_id'])]
            for replay in guide['macro_replay']:
                total += 1
                local = refs[replay['id']]
                self.assertEqual(local, expected_refs[replay['id']])
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

    def test_consecutive_scope_preserves_types_and_stops_at_structural_boundaries(self):
        from botend.services.class_guide_macros import macro_block_references
        code = lambda key: {'id': key, 'type': 'code', 'data': {'code': '/cast Example'}}
        prefix = [code('earlier'), {'id': 'explanation', 'type': 'html',
                  'html': '[[talent:700]] [[item:700]] [[spell:700]]'}]
        expected = [{'kind': kind, 'object_id': 700, 'source_block': 'explanation'}
                    for kind in ('talent', 'item', 'spell')]
        for boundary in ({'id': 'break', 'type': 'html', 'html': '<p>Another explanation</p>'},
                         {'id': 'break', 'type': 'heading', 'title': 'Next'},
                         {'id': 'break', 'type': 'separator'},
                         {'id': 'break', 'type': 'details', 'children': [code('sibling')]}):
            with self.subTest(boundary=boundary['type']):
                rows = prefix + [code('one'), code('two'), code('three'), boundary,
                                 code('after'), {'id': 'later', 'type': 'html', 'html': '[[spell:999]]'}]
                scoped = macro_block_references(rows)
                self.assertEqual(scoped['earlier'], [])
                for key in ('one', 'two', 'three'):
                    self.assertEqual(scoped[key], expected)
                self.assertEqual(scoped['after'], [])
                if 'sibling' in scoped:
                    self.assertEqual(scoped['sibling'], [])
        own = code('own')
        own['html'] = '[[spell:999]]'
        scoped = macro_block_references(prefix + [own, code('next')])
        self.assertEqual(scoped['own'], [{'kind': 'spell', 'object_id': 999, 'source_block': 'own'}])
        self.assertEqual(scoped['next'], expected)

    def test_intro_requires_exact_source_prefix_and_never_borrows_first_prose(self):
        from botend.services.class_guide_macros import macro_block_references
        def para(key, text):
            return {'id': key, 'type': 'html', 'source_type': 'core/paragraph', 'html': text}
        def code(key):
            return {'id': key, 'type': 'code', 'data': {'code': '/cast Example'}}
        quote = {'id': 'intro', 'type': 'html', 'source_type': 'core/quote',
                 'html': '<blockquote><p>[[spell:119898]]</p></blockquote>'}
        rows = [quote, para('first', '[[talent:700]] [[item:700]]'), code('one'), code('two'),
                para('next', 'No identity'), code('three')]
        owner = {'id': 'owner', 'type': 'details', 'source_type': 'advgb/accordion-item', 'children': rows}
        expected = [{'kind': 'spell', 'object_id': 119898, 'source_block': 'intro'}]
        result = macro_block_references([owner])
        self.assertEqual(result['three'], expected)
        self.assertEqual(result['one'], result['two'])
        self.assertEqual([r['kind'] for r in result['one']], ['spell', 'talent', 'item'])
        cases = []
        for field, value in [('type', 'group'), ('source_type', 'unknown')]:
            changed = copy.deepcopy(owner)
            changed[field] = value
            cases.append(changed)
        for field, value in [('source_type', 'core/paragraph'), ('html', '<p>[[spell:119898]]</p>')]:
            changed = copy.deepcopy(owner)
            changed['children'][0][field] = value
            cases.append(changed)
        for prefix in ([code('earlier')] + rows, [quote] + rows[2:],
                       [para('plain', '[[spell:119898]]')] + rows[1:]):
            cases.append(dict(owner, children=prefix))
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(macro_block_references([case])['three'], [])
        for boundary in ({'id': 'empty', 'type': 'details', 'children': []},
                         para('heading', '<h3>Next</h3>')):
            scoped = macro_block_references([
                {'id': 'heading-owner', 'type': 'heading', 'title': '[[spell:119898]]'},
                code('before'), boundary, code('after')])
            self.assertEqual(scoped['before'], [{'kind': 'spell', 'object_id': 119898,
                                                'source_block': 'heading-owner'}])
            self.assertEqual(scoped['after'], [])
        # Multiple ordinary first-description paragraphs are next-run-only.
        ordinary = [para('a', '[[spell:1]]'), para('b', '[[spell:2]]'), code('one'),
                    para('next', 'No identity'), code('three')]
        scoped = macro_block_references(ordinary)
        self.assertEqual([r['object_id'] for r in scoped['one']], [1, 2])
        self.assertEqual(scoped['three'], [])

    def test_frozen_structural_scope_replay(self):
        """Nine real representatives by default; full frozen 43 via env, real ORM."""
        import os
        from collections import Counter
        from botend.models import WowItemSnapshot
        from botend.services.class_guide_macros import (
            macro_block_references, macro_operands, resolve_macro_operands, localize_macro)
        from botend.services.wow_localization import macro_identity_context
        path = os.environ.get('MAXROLL_SCOPE_REPLAY_INPUT')
        frozen = json.loads(Path(path).read_text()) if path else json.loads(
            (Path(__file__).parent / 'fixtures/maxroll_macro_scope.json').read_text())['guides']
        counts, old_counts, changes, ledger = Counter(), Counter(), [], []
        seen, macro_count, contexts_verified = set(), 0, 0
        normalize = lambda value: json.loads(json.dumps(value, sort_keys=True))
        for guide in frozen:
            WowSpellSnapshot.objects.all().delete()
            WowTalentNodeMetadata.all_objects.all().delete()
            WowItemSnapshot.objects.all().delete()
            WowSpellSnapshot.objects.bulk_create([WowSpellSnapshot(**r) for r in guide['snapshots']])
            for raw in guide['raw_talents']:
                row = {k: v for k, v in raw.items() if k not in ('id', 'talent_version_id')}
                WowTalentNodeMetadata.all_objects.create(talent_version=self.version, **row)
            # Item facts in this capture are part of each original context.
            items = {}
            for replay in guide['macro_replay']:
                items.update(replay.get('context', {}).get('items', {}))
            for sid, item in items.items():
                WowItemSnapshot.objects.create(item_id=int(sid), name=item['name'], name_zh=item['name_zh'])
            if not path:
                from botend.services.class_guide_maxroll import convert
                converted, _ = convert({'gutenbergBlock': guide['source_macro_blocks']})
                self.assertEqual(converted, guide['blocks'])
                translated, _ = self.translate(converted)
                for replay in guide['macro_replay']:
                    expected = next((r['output'] for r in INTRO_ORACLE['decisions']
                                     if (r['guide_id'], r['block']) == (guide['guide_id'], replay['id'])),
                                    replay['source_code'])
                    self.assertEqual(translated[replay['id']]['data']['code'], expected)
            refs = macro_block_references(guide['blocks'])
            for replay in guide['macro_replay']:
                key = (guide['guide_id'], replay['id'])
                macro_count += 1
                # Verify frozen DB facts through the actual context builder,
                # not the study's in-memory Query adapter.
                self.assertEqual(normalize(macro_identity_context(replay['source_refs'], self.version)),
                                 normalize(replay['context']), key)
                contexts_verified += 1
                local = refs[replay['id']]
                context = macro_identity_context(local, self.version)
                decisions = resolve_macro_operands(macro_operands(replay['source_code']), context, local)
                output, missing = localize_macro(replay['source_code'], {}, resolve=lambda value, kind:
                    decisions.get((kind, value.casefold()), {}).get('name_zh'))
                for old in replay['decisions']:
                    new = decisions[(old['kind'], old['operand'].casefold())]
                    decision_key = (*key, old['kind'], old['operand'].casefold())
                    self.assertNotIn(decision_key, seen)
                    seen.add(decision_key)
                    oracle = INTRO_DECISIONS[decision_key]
                    baseline = oracle['before']
                    old_counts[baseline['status']] += 1
                    counts[new['status']] += 1
                    entry = {'guide_id': key[0], 'block_id': key[1], 'old': baseline, 'new': new,
                             'output': output, 'missing': missing}
                    ledger.append(entry)
                    if baseline['status'] != new['status']:
                        changes.append(entry)
                    with self.subTest(guide=key[0], block=key[1], operand=old['operand']):
                        # Exact full dictionaries: provenance, typed candidates,
                        # relations, builds, issues, identity and Chinese name.
                        self.assertEqual(normalize(new), oracle['after'])
                        self.assertEqual(output, oracle['output'])
                        self.assertEqual(normalize(new['candidate_identities']), baseline['candidate_identities'])
                        if baseline['status'] == 'resolved':
                            self.assertEqual(new['status'], 'resolved')
                            self.assertEqual(normalize(new['identity']), baseline['identity'])
                            self.assertEqual(new['name_zh'], baseline['name_zh'])
                        if baseline['status'] == 'ambiguous_identity':
                            self.assertEqual(new['status'], 'ambiguous_identity')
        expected_keys = {key for key in INTRO_DECISIONS if key[0] in {g['guide_id'] for g in frozen}}
        self.assertEqual(seen, expected_keys)
        self.assertEqual(len(changes), 13)
        if path:
            self.assertEqual((len(frozen), macro_count, len(seen)), (43, 520, 890))
            self.assertEqual(old_counts, {'resolved': 631, 'missing_identity': 214,
                                         'incomplete_context': 35, 'ambiguous_identity': 10})
            self.assertEqual(counts, {'resolved': 643, 'missing_identity': 215,
                                     'incomplete_context': 22, 'ambiguous_identity': 10})
            self.assertEqual(len({(r['guide_id'], r['block_id']) for r in ledger
                                  if r['new']['status'] != 'resolved'}), 127)
        report = {'guides': len(frozen), 'before': old_counts, 'after': counts,
                  'macro_blocks': macro_count, 'contexts_verified': contexts_verified,
                  'changes': changes, 'decisions': ledger}
        destination = os.environ.get('MAXROLL_SCOPE_REPLAY_OUTPUT')
        if destination:
            Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('SCOPE_REPLAY=' + json.dumps({k: v for k, v in report.items() if k != 'decisions'}, ensure_ascii=False))

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
