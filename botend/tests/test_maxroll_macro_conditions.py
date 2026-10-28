"""Numeric conditions identify only their exact same-line cast, never learning state."""
import copy
import json
import os
from collections import Counter
from pathlib import Path

from django.test import TestCase

from botend.models import WowItemSnapshot, WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion
from botend.services.article_translation_service import ArticleTranslationService
from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_service import translate_blocks
from botend.services.wow_news_glossary_service import WowNewsGlossary

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/maxroll_macro_numeric_known.json').read_text())


class MaxrollMacroConditionTests(TestCase):
    def setUp(self):
        self.version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'major_version': '12.1', 'branch': 'retail', 'is_active': True,
            'current_build': '12.1.5.70575'})
        WowSpellSnapshot.objects.bulk_create([WowSpellSnapshot(**r) for r in FIXTURE['snapshots']])

    def translate(self, code, refs=''):
        service = ArticleTranslationService(engine=False, glossary=WowNewsGlossary.from_trusted_pairs([]))
        blocks = [{'id': 'code', 'type': 'code', 'html': refs, 'data': {'code': code}}]
        result, failed = translate_blocks(blocks, 'retail', service=service)
        return result[0]['data']['code'], [r for r in failed if r['field'] == 'code']

    def test_known_numeric_anchor_and_no_cross_line_identity(self):
        code = '/cast [known:328774] Amplify Curse\r\n/cast Amplify Curse\r\n'
        output, failed = self.translate(code)
        self.assertEqual(output, '/cast [known:328774] 诅咒增幅\r\n/cast Amplify Curse\r\n')
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['operands'][0]['status'], 'missing_identity')
        # Same-name conflict on the next line must stay ambiguous, even if the
        # first line carries another explicit numeric identity.
        WowSpellSnapshot.objects.create(spell_id=900001, branch='wow', locale='zhCN',
            snapshot_build='12.1.5.70575', name='Amplify Curse', name_zh='冲突实体')
        output, failed = self.translate(code, '[[spell:328774]] [[spell:900001]]')
        self.assertIn('/cast Amplify Curse', output)
        self.assertTrue(all(d['status'] == 'ambiguous_identity' for d in failed[0]['operands']))
        for condition in ('known:900001', 'known:328774/900001', 'noknown:328774', 'known:0328774'):
            with self.subTest(condition=condition):
                source = f'/cast [{condition}] Other Spell'
                self.assertEqual(self.translate(source)[0], source)
        WowSpellSnapshot.objects.filter(spell_id=328774).update(name_zh='')
        self.assertEqual(self.translate('/cast [known:328774] Amplify Curse')[0],
                         '/cast [known:328774] Amplify Curse')

    def test_stopmacro_named_conditions_are_real_operands_and_keep_syntax(self):
        row = WowSpellSnapshot.objects.get(spell_id=328774)
        code = '/stopmacro [channeling:Amplify Curse]\n/cast [known:328774] Amplify Curse'
        output, failed = self.translate(code, '[[spell:328774]]')
        self.assertEqual(output, '/stopmacro [channeling:诅咒增幅]\n/cast [known:328774] 诅咒增幅')
        self.assertEqual(failed, [])
        code = '/stopmacro [channeling:Unknown Spell,noknown:328774]\n/run print("Amplify Curse")'
        output, failed = self.translate(code)
        self.assertEqual(output, code)
        self.assertEqual(failed[0]['operands'][0]['operand'], 'Unknown Spell')
        self.assertEqual(row.description, WowSpellSnapshot.objects.get(pk=row.pk).description)

    def test_full43_frozen_real_orm_and_entrypoint(self):
        source = os.environ.get('MAXROLL_CONDITION_REPLAY_INPUT')
        if not source:
            self.skipTest('Full production corpus requires MAXROLL_CONDITION_REPLAY_INPUT')
        from botend.services.class_guide_macros import macro_block_references, localize_macro_identities
        from botend.services.wow_localization import macro_identity_context
        frozen = json.loads(Path(source).read_text())
        oracle = json.loads(Path(os.environ['MAXROLL_CONDITION_ORACLE']).read_text())['decisions']
        expected = {(r['guide_id'], r['block_id'], r['new']['kind'], r['new']['operand'].casefold()): r['new'] for r in oracle}
        counts, seen, gains, ledger, added = Counter(), set(), [], [], []
        blocks_count = 0
        for guide in frozen:
            WowSpellSnapshot.objects.all().delete()
            WowTalentNodeMetadata.all_objects.all().delete()
            WowItemSnapshot.objects.all().delete()
            snapshots = {(r['branch'], r['locale'], r['spell_id']): r for r in guide['snapshots']}
            for row in FIXTURE['snapshots']:
                key = (row['branch'], row['locale'], row['spell_id'])
                if key in snapshots:
                    self.assertEqual(snapshots[key], row)
                snapshots[key] = row
            WowSpellSnapshot.objects.bulk_create([WowSpellSnapshot(**r) for r in snapshots.values()])
            for raw in guide['raw_talents']:
                WowTalentNodeMetadata.all_objects.create(talent_version=self.version,
                    **{k: v for k, v in raw.items() if k not in ('id', 'talent_version_id')})
            items = {}
            for block in guide['macro_replay']:
                items.update(block['context'].get('items', {}))
            for sid, item in items.items():
                WowItemSnapshot.objects.create(item_id=int(sid), name=item['name'], name_zh=item['name_zh'])
            before = list(WowSpellSnapshot.objects.order_by('pk').values())
            service = ArticleTranslationService(engine=False, glossary=WowNewsGlossary.from_trusted_pairs([]))
            translated, failed = translate_blocks(copy.deepcopy(guide['blocks']), 'retail', service=service)
            rendered = {b['id']: b['data']['code'] for b in walk_blocks(translated) if b['type'] == 'code'}
            failed_ids = {r['id'] for r in failed if r['field'] == 'code'}
            scoped = macro_block_references(guide['blocks'])
            for block in guide['macro_replay']:
                blocks_count += 1
                refs = scoped[block['id']]
                load = lambda refs: macro_identity_context(refs, self.version)
                output, missing, decisions = localize_macro_identities(block['source_code'], load(refs), refs, load_context=load)
                self.assertEqual(rendered[block['id']], output)
                self.assertEqual(block['id'] in failed_ids, bool(missing))
                per_operand = {}
                for (_, kind, name), new in decisions.items():
                    key = (guide['guide_id'], block['id'], kind, name)
                    if key not in expected:
                        added.append({'key': key, 'decision': new})
                        continue
                    old = expected[key]
                    seen.add(key)
                    if old['status'] == 'resolved':
                        self.assertEqual((new['status'], list(new['identity']), new['name_zh']),
                                         (old['status'], old['identity'], old['name_zh']))
                    if old['status'] == 'ambiguous_identity':
                        self.assertEqual(new['status'], 'ambiguous_identity')
                        self.assertEqual(new['candidate_ids'], old['candidate_ids'])
                    per_operand[key] = new
                for key, new in per_operand.items():
                    counts[new['status']] += 1
                    if expected[key]['status'] != new['status']:
                        gains.append({'key': key, 'old': expected[key], 'new': new})
                ledger.append({'guide_id': guide['guide_id'], 'block_id': block['id'], 'source': block['source_code'],
                               'output': output, 'missing': missing, 'decisions': list(decisions.values())})
            self.assertEqual(before, list(WowSpellSnapshot.objects.order_by('pk').values()))
        self.assertEqual(seen, set(expected))
        self.assertEqual((len(frozen), blocks_count, len(seen)), (43, 520, 890))
        self.assertEqual(counts, {'resolved': 647, 'missing_identity': 211, 'incomplete_context': 22, 'ambiguous_identity': 10})
        self.assertEqual(len(gains), 4)
        self.assertEqual({r['key'][0] for r in gains}, {142, 143})
        self.assertEqual({tuple(r['key']): tuple(r['decision']['identity']) for r in added}, {
            (132, 'b-98-2-22', 'spell', 'convoke the spirits'): ('spell', 391528),
            (133, 'b-92-2-22', 'spell', 'convoke the spirits'): ('spell', 391528),
            (142, 'b-112-0-6', 'spell', 'channel demonfire'): ('spell', 196447),
            (143, 'b-110-0-6', 'spell', 'channel demonfire'): ('spell', 196447)})
        self.assertTrue(all(r['decision']['status'] == 'resolved' for r in added))
        # Parsing the real priest condition is not license to invent identity.
        for target in FIXTURE['targets']:
            if '/stopmacro [channeling:Void Torrent]' not in target['code']:
                continue
            entry = next(r for r in ledger if (r['guide_id'], r['block_id']) ==
                         (target['guide_id'], target['block_id']))
            self.assertEqual(entry['output'], target['code'])
            self.assertIn('Void Torrent', entry['missing'])
            self.assertEqual(len([d for d in entry['decisions'] if d['operand'] == 'Void Torrent']), 2)
        report = {'guides': len(frozen), 'blocks': blocks_count, 'old_decisions': len(seen), 'statuses': counts, 'added_operands': added,
                  'gains': gains, 'blocks_with_missing': sum(bool(r['missing']) for r in ledger), 'ledger': ledger}
        destination = os.environ.get('MAXROLL_CONDITION_REPLAY_OUTPUT')
        if destination:
            Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('CONDITION_REPLAY=' + json.dumps({k: v for k, v in report.items() if k != 'ledger'}, ensure_ascii=False))
