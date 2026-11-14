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
        self.assertIn('AoE', titles[505])
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
