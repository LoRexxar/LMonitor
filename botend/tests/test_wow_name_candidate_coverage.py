"""Full-document candidate coverage through real localization queries."""
from contextlib import contextmanager
from itertools import islice, product
import sqlite3
import string

from django.db import connection
from django.test import SimpleTestCase, TestCase

from botend.models import (
    WowItemSnapshot, WowSpellSnapshot, WowSpellSnapshotState,
    WowTalentNodeMetadata, WowTalentVersion,
)
from botend.services.class_guide_macros import localize_macro, macro_name_map
from botend.services.wow_localization import effective_names, names_for
from botend.services.wow_news_glossary_service import WowNewsGlossary, _extract_name_candidates


def candidate_words():
    # Digits are not candidate characters. Newlines isolate each unique word.
    return ['Noise' + ''.join(chars) for chars in islice(product(string.ascii_lowercase, repeat=3), 1200)]


@contextmanager
def declared_sqlite_parameter_limit():
    # Exercise SQLite's real SQL parameter ceiling, not a mocked query result.
    connection.ensure_connection()
    old = connection.connection.setlimit(
        sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, connection.features.max_query_params,
    )
    try:
        yield
    finally:
        connection.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old)


class NameCandidateExtractionTests(SimpleTestCase):
    def test_extracts_every_unique_candidate_without_a_count_limit(self):
        words = candidate_words()
        source = '\n'.join(words + words + ['Drain Life', 'Summon Felguard'])
        self.assertSetEqual(
            _extract_name_candidates(source),
            set(words) | {'Drain', 'Life', 'Drain Life', 'Summon', 'Felguard', 'Summon Felguard'},
        )
        self.assertEqual(_extract_name_candidates(''), set())

    def test_protection_checks_tokens_after_the_thousandth_term(self):
        words = candidate_words()
        glossary = WowNewsGlossary.from_trusted_pairs((word, '术语' + word) for word in words)
        protected = glossary.protect('\n'.join(words))
        self.assertEqual(len(protected.replacements), len(words))
        self.assertTrue(protected.is_intact(protected.text))
        self.assertFalse(protected.is_intact(protected.text.replace('⟦WOWTERM_1200⟧', '')))


class NameCandidateDatabaseTests(TestCase):
    def test_shared_names_keep_all_matches_and_late_macro_without_operand_override(self):
        version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults=dict(
            major_version='12.1', branch='retail', current_build='12.1.0.123',
            is_active=True, is_default_player_tree=True,
        ))
        words = candidate_words()
        expected = [(900000 + index, word) for index, word in enumerate(words)]
        expected.extend([(234153, 'Drain Life'), (30146, 'Summon Felguard')])
        WowTalentNodeMetadata.all_objects.bulk_create([
            WowTalentNodeMetadata(talent_version=version, localization_only=True,
                name_kind='spell', reference_id=identity, name=name,
                name_zh={'Drain Life': '吸取生命', 'Summon Felguard': '召唤恶魔卫士'}.get(name, '术语' + name))
            for identity, name in expected
        ])
        # Always-selected macro rows must appear once even if also found by name.
        override = WowTalentNodeMetadata.all_objects.create(
            talent_version=version, localization_only=True, name_kind='macro',
            reference_id=7, name='Other Macro', name_zh='其他宏',
        )
        code = '/cast [@mouseover,exists] Drain Life;Summon Felguard'
        # An unrelated reference selects the actual current-reference branch,
        # but cannot rescue either late spell through an ID lookup.
        source = '\n'.join(words + ['Other Macro', '[[spell:172]]', code])
        with declared_sqlite_parameter_limit():
            rows = names_for('12.0.7', source_text=source)
            effective = effective_names('12.0.7', source_text=source)
        self.assertEqual(
            [(row['object_id'], row['name_en']) for row in rows if row['kind'] == 'spell'], expected,
        )
        self.assertEqual([row['pk'] for row in rows if row['kind'] == 'macro'], [override.pk])
        self.assertEqual(effective, rows)
        names = macro_name_map((row['kind'], row['name_en'], row['name_zh']) for row in effective)
        self.assertEqual(localize_macro(code, names),
                         ('/cast [@mouseover,exists] 吸取生命;召唤恶魔卫士', []))

    def test_spell_snapshot_queries_keep_early_and_late_matches_and_build_scope(self):
        WowSpellSnapshotState.objects.create(branch='wow', locale='zhCN', snapshot_build='12.1-current')
        for identity, name, chinese, build in [
            (30451, 'Arcane Blast', '奥术冲击', '12.1-current'),
            (30146, 'Summon Felguard', '召唤恶魔卫士', '12.1-current'),
            (234153, 'Drain Life', '旧版本译名', '12.0-stale'),
        ]:
            WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=identity,
                name=name, name_zh=chinese, snapshot_build=build)
        source = '\n'.join(['Arcane Blast'] + candidate_words() + ['Summon Felguard', 'Drain Life'])
        with declared_sqlite_parameter_limit():
            glossary = WowNewsGlossary.from_current_spell_metadata(source)
        protected = glossary.protect('Arcane Blast; Summon Felguard; Drain Life')
        self.assertEqual(glossary.restore(protected.text, protected.replacements),
                         '奥术冲击; 召唤恶魔卫士; Drain Life')

    def test_item_snapshot_queries_keep_early_and_late_matches(self):
        WowItemSnapshot.objects.create(item_id=3001, name='Crown of the Cosmos', name_zh='宇宙之冠')
        WowItemSnapshot.objects.create(item_id=3002, name='Zandalar Signet of Might', name_zh='赞达拉力量徽记')
        source = '\n'.join(['Crown of the Cosmos'] + candidate_words() + ['Zandalar Signet of Might'])
        with declared_sqlite_parameter_limit():
            glossary = WowNewsGlossary.from_current_item_metadata(source)
        protected = glossary.protect('Crown of the Cosmos; Zandalar Signet of Might')
        self.assertEqual(glossary.restore(protected.text, protected.replacements),
                         '宇宙之冠; 赞达拉力量徽记')
