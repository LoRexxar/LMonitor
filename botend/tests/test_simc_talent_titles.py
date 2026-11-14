"""Read-only title projection, using six imported codes and real guide excerpts."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from botend.guide_models import ClassGuide
from botend.models import SimcTalentString
from botend.services.simc_talent_titles import (
    apply_talent_display_names, frozen_talent_display_name, frozen_talent_display_names, talent_display_name,
)


class TalentDisplayTitleTests(TestCase):
    def setUp(self):
        from botend.services.simc_talent_titles import _GUIDE_CACHE
        _GUIDE_CACHE.clear()

    @classmethod
    def setUpTestData(cls):
        evidence = json.loads((Path(__file__).parent / 'fixtures' / 'simc_talent_titles_icy.json').read_text())
        cls.resources = evidence['resources']
        guides = {}
        for occurrence in evidence['occurrences']:
            guide_id = occurrence['guide_id']
            guides.setdefault(guide_id, {'row': occurrence, 'parts': []})['parts'].append(
                '\n\n'.join('#' * (index + 2) + ' ' + title
                            for index, title in enumerate(occurrence['heading_path']))
                + '\n\n' + occurrence['raw_block'])
        for guide_id, guide in guides.items():
            row = guide['row']
            ClassGuide.objects.create(pk=guide_id, title='Evidence', slug=f'evidence-{guide_id}',
                spec_id=row['spec_id'], class_name=row['class_name'], spec_name=row['spec_name'],
                game_version='12.1.5', source_url='https://www.icy-veins.com/wow/test',
                source_markdown='\n\n'.join(guide['parts']))
        cls.ids = []
        for row in cls.resources:
            obj = SimcTalentString.objects.create(**{key: row[key] for key in
                ('name', 'spec', 'talent', 'hero_talent_names', 'system_key')})
            cls.ids.append(obj.pk)

    def test_six_same_name_variants_batch_source_and_no_mutation(self):
        rows = list(SimcTalentString.objects.filter(pk__in=self.ids).order_by('pk'))
        before = [{key: (value if key == '_state' else copy.deepcopy(value))
                   for key, value in row.__dict__.items()} for row in rows]
        with self.assertNumQueries(2):
            self.assertIs(apply_talent_display_names(rows), rows)
        expected = [
            ('750b3e9f', '天赋与构筑', 'Frostfire Mythic+ Talents'),
            ('2394bacb', '大秘境', 'Frostfire Mythic+ Talents'),
            ('5b9e9bb2', '天赋与构筑', 'Defensive Mythic+ Build'),
            ('4654546c', '大秘境', 'Defensive Mythic+ Build'),
            ('af58c38d', '天赋与构筑', 'Outlaw Single Target - Trickster'),
            ('6d775aa0', '天赋与构筑', 'Outlaw AoE - Trickster'),
        ]
        for row, old, (short, chapter, label) in zip(rows, before, expected):
            with self.subTest(short=short):
                title = row.display_name
                for value in ('[Icy Veins]', chapter, label, row.hero_talent_names[0]):
                    self.assertIn(value, title)
                self.assertNotIn(short, title)
                for value in ('Fatebound', '![]', 'https://', '.webp'):
                    self.assertNotIn(value, title)
                self.assertEqual({k: v for k, v in row.__dict__.items() if k != 'display_name'}, old)
        persisted = list(SimcTalentString.objects.filter(pk__in=self.ids).order_by('pk').values('name', 'talent', 'system_key'))
        self.assertEqual(persisted, [{k: old[k] for k in ('name', 'talent', 'system_key')} for old in before])
        dictionaries = copy.deepcopy(self.resources)
        with self.assertNumQueries(1):
            self.assertIs(apply_talent_display_names(dictionaries), dictionaries)
        self.assertEqual([r.display_name for r in rows], [r['display_name'] for r in dictionaries])
        self.assertEqual([{k: v for k, v in r.items() if k != 'display_name'} for r in dictionaries], self.resources)

    def test_plain_fallback_cleans_markdown_and_html_without_lookup(self):
        row = {'name': '[Icy Veins] ![icon](<https://x.test/a(b).webp>) [Readable **build**](https://x.test/build) <img src="x"> · deadbeef'}
        original = copy.deepcopy(row)
        with self.assertNumQueries(0):
            title = talent_display_name(row)
        self.assertEqual(title, '[Icy Veins] Readable build')
        self.assertEqual(row, original)
        self.assertEqual(talent_display_name({'name': '[Maxroll] 欺诈者 · Raid Build · 79f99009'}),
                         '[Maxroll] 欺诈者 · Raid Build')
        self.assertEqual(talent_display_name({'name': 'Personal · deadbeef'}), 'Personal · deadbeef')
        self.assertEqual(frozen_talent_display_name({'name': 'Profile · [Maxroll] Raid Build · 79f99009'}),
                         'Profile · [Maxroll] Raid Build')

    def test_frozen_payload_uses_only_matching_identity_and_never_current_name(self):
        row = self.resources[4]
        payload = {key: copy.deepcopy(row[key]) for key in ('name', 'spec', 'talent', 'hero_talent_names')}
        original = copy.deepcopy(payload)
        current = SimcTalentString.objects.get(pk=self.ids[4])
        current.name = 'DO NOT USE CURRENT NAME'
        current.save(update_fields=['name'])
        with self.assertNumQueries(3):
            title = frozen_talent_display_name(payload, current.pk)
        self.assertIn('Outlaw Single Target - Trickster', title)
        self.assertNotIn('DO NOT USE CURRENT NAME', title)
        current.talent = self.resources[5]['talent']
        current.save(update_fields=['talent'])
        with self.assertNumQueries(1):
            title = frozen_talent_display_name(payload, current.pk)
        self.assertEqual(title, payload['name'].rsplit(' · ', 1)[0])
        self.assertEqual(payload, original)
        # No ID is also supported: exact spec/code lookup, not name matching.
        with self.assertNumQueries(2):
            title = frozen_talent_display_name({key: self.resources[5][key] for key in payload})
        self.assertIn('Outlaw AoE - Trickster', title)

    def test_frozen_page_batches_identity_and_guide_reads(self):
        from botend.services.simc_talent_titles import _occurrences
        payloads = [{key: copy.deepcopy(row[key]) for key in
                     ('name', 'spec', 'talent', 'hero_talent_names')} for row in self.resources]
        before = copy.deepcopy(payloads)
        with self.assertNumQueries(3), patch(
                'botend.services.simc_talent_titles._occurrences', wraps=_occurrences) as compile_source:
            titles = frozen_talent_display_names(payloads, self.ids)
        self.assertEqual(compile_source.call_count, 3)
        self.assertEqual(payloads, before)
        self.assertEqual(len(titles), 6)
        self.assertIn('Outlaw Single Target - Trickster', titles[4])
        self.assertIn('Outlaw AoE - Trickster', titles[5])
        self.assertNotIn('https://', ''.join(titles))
        # Swapped resource IDs must not decorate a historical code with its peer.
        with self.assertNumQueries(1):
            swapped = frozen_talent_display_names(payloads[4:], list(reversed(self.ids[4:])))
        self.assertEqual(swapped, [payload['name'].rsplit(' · ', 1)[0] for payload in payloads[4:]])

    def test_bad_provenance_missing_code_or_broken_guide_falls_back(self):
        row = copy.deepcopy(self.resources[0])
        for system_key in (row['system_key'] + ':extra', row['system_key'].replace('mage_fire', 'mage_frost'),
                           row['system_key'][:-1] + ('0' if row['system_key'][-1] != '0' else '1')):
            with self.subTest(key=system_key), self.assertNumQueries(0):
                title = talent_display_name(dict(row, system_key=system_key))
                self.assertNotIn('天赋与构筑', title)
                self.assertNotIn('https://', title)
        for source in (':::unknown\nbroken\n:::', '## No matching code'):
            ClassGuide.objects.filter(pk=83).update(source_markdown=source)
            with self.assertNumQueries(2):
                self.assertNotIn('天赋与构筑', talent_display_name(row))
        ClassGuide.objects.filter(pk=83).update(spec_id=64, spec_name='Frost')
        with self.assertNumQueries(2):
            self.assertNotIn('天赋与构筑', talent_display_name(row))

    def test_warm_projection_avoids_body_transfer_and_source_change_invalidates(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        row = copy.deepcopy(self.resources[0])
        first = talent_display_name(row)
        with CaptureQueriesContext(connection) as queries, patch(
                'botend.services.simc_talent_titles._occurrences', side_effect=AssertionError('warm source recompiled')):
            self.assertEqual(talent_display_name(row), first)
        self.assertEqual(len(queries), 1)
        self.assertIn('SHA256', queries[0]['sql'].upper())
        self.assertNotIn('"source_markdown",', queries[0]['sql'])
        guide = ClassGuide.objects.get(pk=83)
        guide.source_markdown = '## 大秘境\n\n:::talents Frostfire revised build\n```json\n' + json.dumps({'code': row['talent']}) + '\n```\n:::'
        guide.save(update_fields=['source_markdown'])
        # The unrelated source_hash was deliberately not changed.
        title = talent_display_name(row)
        self.assertIn('Frostfire revised build', title)
        self.assertIn('大秘境', title)
        self.assertNotEqual(title, first)

    def test_conflicting_hero_label_is_rejected_and_order_is_stable(self):
        row = copy.deepcopy(self.resources[4])
        guide = ClassGuide.objects.get(pk=105)
        # Keep only the misleading quick-start occurrence: do not advertise Fatebound.
        guide.source_markdown = '## 天赋与构筑\n\n:::talents Fatebound Raid Build\n```json\n' + json.dumps({'code': row['talent']}) + '\n```\n:::'
        guide.save(update_fields=['source_markdown'])
        title = talent_display_name(row)
        self.assertNotIn('Fatebound', title)
        self.assertIn('欺诈者', title)
        self.assertIn('天赋与构筑', title)
        self.assertNotIn('af58c38d', title)
        self.assertEqual(title, talent_display_name(row))




class ChineseTalentDisplayTitleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.evidence = json.loads((Path(__file__).parent / 'fixtures' / 'simc_talent_titles_zh.json').read_text())
        for guide in cls.evidence['guides']:
            ClassGuide.objects.create(**guide, title='Bilingual evidence', slug=f"zh-{guide['id']}")
        cls.ids = [SimcTalentString.objects.create(**{k: row[k] for k in
                   ('name', 'spec', 'talent', 'hero_talent_names', 'system_key')}).pk
                   for row in cls.evidence['resources']]

    def setUp(self):
        from botend.services.simc_talent_titles import _GUIDE_CACHE
        _GUIDE_CACHE.clear()

    def test_real_bilingual_occurrences_cache_and_frozen_inputs(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        rows = copy.deepcopy(self.evidence['resources'])
        before = copy.deepcopy(rows)
        with self.assertNumQueries(2):
            apply_talent_display_names(rows)
        titles = [r['display_name'] for r in rows]
        self.assertIn('大秘境天赋', titles[0])
        self.assertIn('狂徒 单体目标', titles[1])
        self.assertIn('狂徒 范围伤害', titles[2])
        for title, hero in zip(titles, ('霜火', '欺诈者', '欺诈者')):
            self.assertEqual(title.count(hero), 1)
            self.assertNotRegex(title, 'Frostfire|Trickster|Fatebound|宝库')
        self.assertEqual([{k: v for k, v in r.items() if k != 'display_name'} for r in rows], before)
        payloads = [{k: r[k] for k in ('name', 'spec', 'talent', 'hero_talent_names')} for r in before]
        frozen = copy.deepcopy(payloads)
        self.assertEqual(frozen_talent_display_names(payloads, self.ids), titles)
        self.assertEqual(payloads, frozen)
        with CaptureQueriesContext(connection) as queries, patch(
                'botend.services.simc_talent_titles._occurrences', side_effect=AssertionError('warm parse')):
            self.assertEqual(talent_display_name(rows[1]), titles[1])
        self.assertEqual(len(queries), 1)
        self.assertNotIn('"content_markdown",', queries[0]['sql'])
        guide = ClassGuide.objects.get(pk=105)
        ClassGuide.objects.filter(pk=105).update(content_markdown=guide.content_markdown.replace('狂徒 单体目标', '狂徒 单体修订'))
        self.assertIn('单体修订', talent_display_name(rows[1]))
        self.assertEqual(ClassGuide.objects.get(pk=105).source_markdown, guide.source_markdown)
        self.assertEqual(list(SimcTalentString.objects.order_by('pk').values_list('name', flat=True)),
                         [r['name'] for r in before])

    def test_title_read_reuses_code_adapter_without_rendering_components(self):
        from botend.services import simc_talent_titles as titles
        from botend.services.class_guide_markdown import compile_markdown
        row = self.evidence['resources'][0]
        block = ':::talents Frostfire Single Target\n```json\n' + json.dumps({'code': row['talent']}) + '\n```\n:::'
        expected = compile_markdown(block)[0]['data']['code']
        with patch('botend.services.class_guide_markdown.compile_markdown', side_effect=AssertionError('title read rendered a component')), patch.object(
                titles, 'prepare_talent_data', wraps=titles.prepare_talent_data) as adapter:
            result = titles._occurrences('## 天赋与构筑\n\n' + block + '\n\n' + block)
        self.assertEqual(list(result), [expected])
        self.assertEqual(len(result[expected]), 2)
        self.assertEqual(adapter.call_count, 1)
        self.assertTrue(all(item['label'] == 'Frostfire Single Target' for item in result[expected]))

    def test_translated_caption_cannot_override_frozen_hero(self):
        from botend.services.simc_talent_titles import _occurrences, _pair_content, _identity, _project
        row = copy.deepcopy(self.evidence['resources'][0])
        block = '\n```json\n' + json.dumps({'code': row['talent']}) + '\n```\n:::'
        source = _occurrences('## 天赋与构筑\n\n:::talents Frostfire Single Target' + block)
        content = _occurrences('## 天赋与构筑\n\n:::talents 日怒 单体目标' + block)
        _pair_content(source, content)
        identity = _identity(row)
        title = _project(row, identity, (identity[1], source, 'icy-veins', '', {}))
        self.assertIn('霜火', title)
        self.assertNotIn('日怒', title)

    def test_numbered_description_uses_its_own_section_and_scheme(self):
        from botend.services.simc_talent_titles import _occurrences, _pair_content, _identity, _project
        case = self.evidence['description_case']
        source = _occurrences(case['source_markdown'])
        content = _occurrences(case['content_markdown'])
        _pair_content(source, content)
        row = case['resource']
        identity = _identity(row)
        title = _project(row, identity, (identity[1], source, 'icy-veins', '', {}))
        self.assertIn('纯单体目标配装', title)
        self.assertEqual(title.count('日怒'), 1)
        self.assertNotRegex(title, 'Single|Build|疾咒师|大秘境')

    def test_real_structure_preserves_descriptions_and_encounter_names(self):
        from botend.services.simc_talent_titles import _occurrences, _pair_content, _identity, _project
        titles = {}
        for case in self.evidence['structure_cases']:
            source = _occurrences(case['source_markdown'])
            _pair_content(source, _occurrences(case['content_markdown']),
                          case['source_markdown'], case['content_markdown'])
            for row in case['resources']:
                identity = _identity(row)
                titles[row['id']] = _project(row, identity, (identity[1], source, 'icy-veins', '', {}))
        expected = {233: '多目标团队副本天赋', 234: '大秘境地下城天赋',
                    237: '大秘境地下城与地下堡天赋', 241: '单体目标团队副本天赋',
                    242: '多目标团本天赋', 243: '多目标团队副本天赋',
                    244: '史诗钥石地下城与宝库天赋',
                    366: '团本 - 乌拉特克（英雄）', 367: '团本 - 乌拉特克（史诗）',
                    368: '范围伤害 - 无第2赛季4件套',
                    371: '团本 - 陵寝哨兵', 376: '团队副本构建'}
        self.assertEqual(set(titles), set(expected))
        for resource_id, phrase in expected.items():
            with self.subTest(resource_id=resource_id):
                self.assertIn(phrase, titles[resource_id])
                self.assertNotIn('灵活天赋', titles[resource_id])
        self.assertNotIn('地下堡', titles[368])
        self.assertEqual(len(set(titles.values())), len(titles))

    def test_description_stops_at_first_heading_or_talent_component(self):
        from botend.services.simc_talent_titles import _occurrences
        row = self.evidence['description_case']['resource']
        block = ':::talents 天赋构筑\n```json\n' + json.dumps({'code': row['talent']}) + '\n```\n:::'
        section = '## 天赋与构筑\n\n### 天赋方案 0-0\n\n'
        for body, expected in (
                ('#### 日怒 纯单体目标配装\n\n#### 灵活天赋\n\n' + block, '日怒 纯单体目标配装'),
                (block + '\n\n#### 后续天赋讨论', ''),
                ('#### 日怒 纯单体目标配装\n\n' + section + '#### 日怒 多目标配装\n\n' + block, '')):
            with self.subTest(body=body):
                matches = _occurrences(section + body)[row['talent']]
                self.assertTrue(all(item['description_label'] == expected for item in matches))

    def test_same_href_translation_requires_unambiguous_local_evidence(self):
        from botend.services.simc_talent_titles import _link_translations, _translate_link_text, _occurrences, _pair_content
        source = "[Ula'tek](<https://example.test/encounter>)"
        content = '[乌拉特克](<https://example.test/encounter>)'
        translations = _link_translations(source, content)
        for spelling in ('Ulatek', "Ula'tek", 'Ula’tek'):
            self.assertEqual(_translate_link_text(f'团本 - {spelling}（英雄）', translations), '团本 - 乌拉特克（英雄）')
        for text in ('Ulatek Raid', '团本 - Ulateks', '团本 - RB'):
            self.assertEqual(_translate_link_text(text, translations), text)
        self.assertEqual(_link_translations(source, content.replace('/encounter', '/other')), {})
        self.assertEqual(_link_translations(source + '\n\n[Other](<https://example.test/encounter>)',
                                            content, needed_terms={'ulatek'}), {})
        escaped_source = '[Ulatek](<https://example.test/a(b)>)\n\n[Other](https://example.test/a\\(b\\))'
        self.assertEqual(_link_translations(escaped_source, '[乌拉特克](<https://example.test/a(b)>)',
                                            needed_terms={'ulatek'}), {})
        self.assertEqual(_link_translations(source, content + '\n\n[另一译名](<https://example.test/encounter>)'), {})
        self.assertEqual(_link_translations(source + '\n\n' + source.replace('/encounter', '/other'),
                         content + '\n\n[另一译名](<https://example.test/other>)'), {})
        # The slug cannot supply an absent English link name, nor can a fenced example.
        self.assertEqual(_link_translations('[攻略](<https://example.test/ulatek>)',
                                          '[乌拉特克](<https://example.test/ulatek>)'), {})
        self.assertEqual(_link_translations('```text\n' + source + '\n```', content), {})
        case = self.evidence['structure_cases'][-1]
        original = _occurrences(case['source_markdown'])
        mismatched = _occurrences(case['content_markdown'])
        mismatched.pop(next(iter(mismatched)))
        _pair_content(original, mismatched, case['source_markdown'], case['content_markdown'])
        self.assertTrue(all('translated' not in item for matches in original.values() for item in matches))

    def test_hero_boundaries_preserve_entities_and_cleanup_only_removed_heroes(self):
        from botend.services.simc_talent_titles import _hero_mentions, _without_heroes, _maxroll_contexts
        for entity in ('Entombed Sentinels', '陵寝哨兵', 'Sentinels', '巨像守卫'):
            with self.subTest(entity=entity):
                self.assertEqual(_hero_mentions(entity), set())
                self.assertEqual(_without_heroes(entity, ['哨兵', '巨神兵']), entity)
                occurrence = {'path': ('天赋与构筑', entity), 'label': '天赋构筑', 'order': 1}
                self.assertEqual(_maxroll_contexts({'code': [occurrence]})['code'], entity)
        self.assertEqual(_hero_mentions('Sentinel / 哨兵 / 巨像'), {'哨兵', '巨神兵'})
        self.assertEqual(_hero_mentions('Voidweaver Raid - Entombed Sentinels'), {'虚空编织者'})
        self.assertEqual(_without_heroes('虚空编织者团本 - 陵寝哨兵', ['虚空编织者']), '团本 - 陵寝哨兵')
        self.assertEqual(_without_heroes('AoE/大秘境 天赋方案，适用于 霜火', ['霜火']), '范围伤害/大秘境 天赋方案')
        self.assertEqual(_without_heroes('大秘境/AoE——荒野追猎者', ['荒野追猎者']), '大秘境/范围伤害')
        self.assertEqual(_without_heroes('Mythic+/M+ AOE天赋', []), '大秘境/大秘境 范围伤害天赋')
        self.assertEqual(_without_heroes('RB Ulatek Mythic+Extra MAoE', []), 'RB Ulatek Mythic+Extra MAoE')
        self.assertEqual(_without_heroes('适用于陵寝哨兵', ['哨兵']), '适用于陵寝哨兵')
        occurrence = {'path': ('天赋与构筑', '- 单体'), 'label': '天赋构筑', 'order': 1}
        self.assertEqual(_maxroll_contexts({'code': [occurrence]})['code'], '单体')

    def test_maxroll_localized_contexts_keep_source_order(self):
        from botend.services.simc_talent_titles import _occurrences, _pair_content, _maxroll_contexts
        for case in self.evidence['maxroll_cases']:
            source = _occurrences(case['source_markdown'])
            content = _occurrences(case['content_markdown'])
            _pair_content(source, content)
            contexts = _maxroll_contexts(source)
            self.assertEqual(contexts, _maxroll_contexts(dict(reversed(list(source.items())))))
            self.assertNotIn('Talents', ' '.join(contexts.values()))
            # Two distinct source codes with the same translated context stay distinct.
            self.assertEqual(len(set(contexts.values())), len(contexts))

    def test_sequence_mismatch_refuses_translation(self):
        from botend.services.simc_talent_titles import _occurrences, _pair_content
        guide = self.evidence['guides'][1]
        source = _occurrences(guide['source_markdown'])
        content = _occurrences(guide['content_markdown'])
        code = next(iter(content))
        content[code].pop()
        _pair_content(source, content)
        self.assertTrue(all('translated' not in o for matches in source.values() for o in matches))


class MaxrollTalentDisplayTitleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence = json.loads((Path(__file__).parent / 'fixtures' / 'simc_talent_titles_maxroll.json').read_text())
        cls.resources = evidence['resources']
        for guide in evidence['guides']:
            parts = ['\n\n'.join('#' * (i + 2) + ' ' + title for i, title in enumerate(o['heading_path']))
                     + '\n\n' + o['raw_block'] for o in evidence['occurrences'] if o['guide_id'] == guide['id']]
            ClassGuide.objects.create(**guide, title='Evidence', slug=f"maxroll-{guide['id']}",
                                      source_markdown='\n\n'.join(parts))
        for row in cls.resources:
            SimcTalentString.objects.create(**row)

    def setUp(self):
        from botend.services.simc_talent_titles import _GUIDE_CACHE
        _GUIDE_CACHE.clear()

    def test_real_contexts_frozen_order_and_immutability(self):
        rows = copy.deepcopy(self.resources)
        before = copy.deepcopy(rows)
        with self.assertNumQueries(2):
            apply_talent_display_names(rows)
        titles = {r['id']: r['display_name'] for r in rows}
        self.assertIn('范围伤害', titles[505])
        self.assertNotIn('cleave', titles[505].lower())
        self.assertIn('Cleave', titles[506])
        self.assertIn('方案1', titles[544])
        self.assertIn('方案2', titles[545])
        self.assertIn('大秘境', titles[629])
        self.assertNotIn('Raids', titles[629])
        self.assertIn('团本', titles[631])
        self.assertIn('命缚者', titles[630])
        self.assertIn('方案', titles[630])
        for row in rows:
            self.assertNotRegex(row['display_name'], r'\[\[|<|https://|·\s*·|·$|[0-9a-f]{8}$')
            self.assertEqual({k: v for k, v in row.items() if k != 'display_name'}, before[rows.index(row)])
        with self.assertNumQueries(1):
            reversed_rows = apply_talent_display_names(list(reversed(copy.deepcopy(before))))
        self.assertEqual(titles, {r['id']: r['display_name'] for r in reversed_rows})
        payloads = [{k: r[k] for k in ('name', 'spec', 'talent', 'hero_talent_names')} for r in before]
        frozen_before = copy.deepcopy(payloads)
        with self.assertNumQueries(2):
            self.assertEqual(frozen_talent_display_names(payloads), list(titles.values()))
        self.assertEqual(payloads, frozen_before)

    def test_frozen_source_is_not_switched_by_an_identical_code(self):
        row = copy.deepcopy(self.resources[0])
        expected = talent_display_name(row)
        other = dict(row, id=1, name=row['name'].replace('[Maxroll]', '[Icy Veins]'),
                     system_key=row['system_key'].replace('guide:maxroll:', 'guide:icy-veins:'))
        SimcTalentString.objects.create(**other)
        payload = {k: row[k] for k in ('name', 'spec', 'talent', 'hero_talent_names')}
        self.assertEqual(frozen_talent_display_name(payload), expected)
        self.assertEqual(frozen_talent_display_name(payload, resource_id=1),
                         row['name'].rsplit(' · ', 1)[0])

    def test_metadata_and_provenance_fences(self):
        row = copy.deepcopy(self.resources[0])
        first = talent_display_name(row)
        guide_id = int(row['system_key'].split(':')[2])
        ClassGuide.objects.filter(pk=guide_id).update(guide_type='mythic-plus')
        self.assertIn('大秘境', talent_display_name(row))
        self.assertNotEqual(first, talent_display_name(row))
        for changes in ({'source_url': 'https://maxroll.gg.evil.test/wow'},
                        {'source_url': 'https://www.icy-veins.com/wow'},
                        {'source_url': 'https://maxroll.gg/wow', 'spec_id': 260,
                         'class_name': 'Rogue', 'spec_name': 'Outlaw'}):
            ClassGuide.objects.filter(pk=guide_id).update(**changes)
            self.assertEqual(talent_display_name(row), row['name'].rsplit(' · ', 1)[0])
        for field, value in [('talent', row['talent'] + 'A'), ('spec', 'mage_fire')]:
            with self.assertNumQueries(0):
                self.assertEqual(talent_display_name(dict(row, **{field: value})), row['name'].rsplit(' · ', 1)[0])
