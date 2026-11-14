"""Real central-name ORM coverage for the narrow Fury RB title projection."""
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.services import wow_localization
from botend.services.simc_talent_title_terms import localize_imported_title_terms


class ImportedTitleTermsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.version, _ = WowTalentVersion.objects.update_or_create(
            key='retail', defaults={
                'branch': 'retail', 'is_active': True, 'current_build': 'test-retail'})
        cls.node = WowTalentNodeMetadata.objects.create(
            talent_version=cls.version, localization_only=False, is_current=True,
            class_name='Warrior', spec_name='Fury', node_id=1001,
            spell_id=85288, display_spell_id=85288,
            name='Raging Blow', name_zh='怒击')

    def row(self, title='[Maxroll] 山丘领主 · 大秘境 · RB 范围伤害', **overrides):
        return {
            'name': 'Original RB title', 'spec': 'warrior_fury',
            'system_key': 'guide:maxroll:162:warrior_fury:' + 'a' * 64,
            'talent': 'frozen talent code', 'display_name': title, **overrides,
        }

    def test_batch_replaces_only_display_name_with_one_readonly_central_call(self):
        rows = [self.row(), self.row('[Icy Veins] RB / (RB)',
                system_key='guide:icy-veins:89:warrior_fury:' + 'b' * 64)] * 20
        before = deepcopy(rows)
        with patch.object(wow_localization, 'effective_names',
                          wraps=wow_localization.effective_names) as names:
            with CaptureQueriesContext(connection) as queries:
                result = localize_imported_title_terms(rows)
        self.assertIs(result, rows)
        names.assert_called_once_with(
            'retail', class_name='Warrior', spec_name='Fury',
            reference_ids={'spell': {85288}})
        self.assertTrue(queries.captured_queries)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT')
                            for q in queries.captured_queries))
        for original, row in zip(before, rows):
            self.assertEqual(row, {**original, 'display_name':
                                  original['display_name'].replace('RB', '怒击')})

    def test_word_boundaries_leave_longer_identifiers_untouched(self):
        rows = [self.row('RB (RB) RB/RB RB-build XRB RBX RB2 2RB _RB RB_ rb')]
        localize_imported_title_terms(rows)
        self.assertEqual(rows[0]['display_name'],
                         '怒击 (怒击) 怒击/怒击 怒击-build XRB RBX RB2 2RB _RB RB_ rb')

    def test_other_specs_custom_and_malformed_sources_need_no_lookup(self):
        rows = [self.row(spec='warrior_arms'), self.row(spec='Warrior_Fury'),
                self.row(system_key=''), self.row(system_key=None),
                self.row(system_key='custom:RB'),
                self.row(system_key='guide:maxroll:162:warrior_arms:' + 'a' * 64),
                self.row(system_key='guide:maxroll:162:warrior_fury:bad'),
                self.row(system_key='guide:maxroll-pretend:162:warrior_fury:' + 'a' * 64),
                self.row(system_key='guide:icy-veins:0:warrior_fury:' + 'a' * 64),
                self.row('Already localized'), self.row(display_name=None)]
        before = deepcopy(rows)
        with patch.object(wow_localization, 'effective_names',
                          wraps=wow_localization.effective_names) as names:
            with self.assertNumQueries(0):
                self.assertEqual(localize_imported_title_terms(rows), before)
                self.assertEqual(localize_imported_title_terms([]), [])
        names.assert_not_called()

    def test_missing_chinese_does_not_borrow_ptr(self):
        ptr, _ = WowTalentVersion.objects.update_or_create(
            key='ptr', defaults={'branch': 'ptr', 'is_active': True})
        WowTalentNodeMetadata.all_objects.create(
            talent_version=ptr, localization_only=True, name_kind='spell',
            reference_id=85288, name='Raging Blow', name_zh='测试服译名')
        for translation in ('', '   ', 'Raging Blow'):
            with self.subTest(translation=translation):
                WowTalentNodeMetadata.objects.filter(pk=self.node.pk).update(name_zh=translation)
                rows = [self.row()]
                before = deepcopy(rows)
                self.assertEqual(localize_imported_title_terms(rows), before)

    def test_same_id_unique_chinese_and_conflict(self):
        duplicate = WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Warrior', spec_name='Fury',
            node_id=1002, spell_id=85288, name='Raging Blow', name_zh='怒击')
        rows = [self.row('RB')]
        localize_imported_title_terms(rows)
        self.assertEqual(rows[0]['display_name'], '怒击')
        duplicate.name_zh = '冲突译名'
        duplicate.save(update_fields=['name_zh'])
        rows = [self.row('RB')]
        before = deepcopy(rows)
        self.assertEqual(localize_imported_title_terms(rows), before)

    def test_only_exact_english_and_spell_identity_supply_names(self):
        WowTalentNodeMetadata.objects.filter(pk=self.node.pk).update(name='Other Spell')
        WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Warrior', spec_name='Fury',
            node_id=1003, spell_id=99999, name='Raging Blow', name_zh='错误编号译名')
        rows = [self.row()]
        before = deepcopy(rows)
        self.assertEqual(localize_imported_title_terms(rows), before)

    def test_next_batch_reads_updated_central_translation(self):
        rows = [self.row('RB')]
        localize_imported_title_terms(rows)
        self.assertEqual(rows[0]['display_name'], '怒击')
        WowTalentNodeMetadata.objects.filter(pk=self.node.pk).update(name_zh='中央新译名')
        rows = [self.row('RB')]
        localize_imported_title_terms(rows)
        self.assertEqual(rows[0]['display_name'], '中央新译名')
