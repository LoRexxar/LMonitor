"""News consumes verified, scoped Journal/SpellName facts, not name guesses."""
import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import WowSpellSnapshot, WowSpellSnapshotState, WowTalentVersion
from botend.services.article_translation_service import ArticleTranslationService
from botend.services.wow_news_glossary_service import WowNewsGlossary


class NewsJournalGlossaryTests(TestCase):
    relation_build = '12.1.0.69587'
    name_build = '12.1.5.70783'

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings_override = override_settings(BASE_DIR=self.root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.directory = self.root / '.cache/adventure-journal' / self.relation_build / 'zhCN'
        self.directory.mkdir(parents=True)
        self.release = JournalRelease.objects.create(
            build=self.relation_build, status='completed',
            manifest={'retail_build': self.relation_build, 'tables': {}},
        )
        JournalState.objects.create(active_release=self.release)
        self.raid = JournalInstance.objects.create(
            release=self.release, journal_id=1320, name='烈毒之渊', kind='raid',
        )
        self.twin = self.boss(2887, '双子毒牙', 3421, [
            {'id': 101, 'spell_id': 1308385, 'title': '脏腑爆裂'},
        ])
        self.vashnik = self.boss(2882, '万毒邪祟者瓦什尼克', 3455, [
            {'id': 102, 'spell_id': 1304459, 'title': '恶念'},
        ])
        self.ula = self.boss(2895, '乌拉特克', 3492, [
            {'id': 103, 'spell_id': 1290779, 'title': '恶意', 'source_text': '|Hspell:1284208|h[$@spellname1292188]|h'},
            {'id': 104, 'spell_id': 0, 'title': '机制标题'},
        ])
        self.table('JournalInstance', ['ID', 'Name_lang'], [(1320, 'The Venomous Abyss')])
        self.table('JournalInstance', ['ID', 'Name_lang'], [(1320, '烈毒之渊')], locale='zhCN')
        self.table('DungeonEncounter', ['ID', 'Name_lang'], [
            (3421, 'The Twin Fangs'), (3455, 'Vashnik the Malignant'), (3492, "Ula'tek"),
        ], release_proof=True)
        self.table('JournalEncounter', ['ID', 'Name_lang', 'JournalInstanceID'], [
            (2887, 'The Twin Fangs', 1320), (2882, 'Vashnik the Malignant', 1320), (2895, "Ula'tek", 1320),
        ])
        self.table('JournalEncounter', ['ID', 'Name_lang', 'JournalInstanceID'], [
            (2895, '乌拉特克', 1320), (2882, '万毒邪祟者瓦什尼克', 1320), (2887, '双子毒牙', 1320),
        ], locale='zhCN')
        self.table('JournalEncounterSection', ['ID', 'Title_lang', 'BodyText_lang', 'JournalEncounterID', 'SpellID'], [
            (104, 'Mechanism Title', '', 2895, 0), (103, '$@spellname1290779', '|Hspell:1284208|h[$@spellname1292188]|h', 2895, 1290779),
            (102, 'Malignance', '', 2882, 1304459), (101, 'Visceral Burst', '', 2887, 1308385),
        ])
        self.table('JournalEncounterSection', ['ID', 'Title_lang', 'BodyText_lang', 'JournalEncounterID', 'SpellID'], [
            (101, '脏腑爆裂', '', 2887, 1308385), (102, '恶念', '', 2882, 1304459),
            (103, '$@spellname1290779', '', 2895, 1290779), (104, '机制标题', '', 2895, 0),
        ], locale='zhCN')
        self.table('JournalEncounterCreature', ['ID', 'Name_lang', 'JournalEncounterID'], [
            (301, 'Blightscale Viper', 2895), (302, 'Blightscale Rawling', 2895),
        ])
        self.table('JournalEncounterCreature', ['ID', 'Name_lang', 'JournalEncounterID'], [
            (302, '疫鳞幼体', 2895), (301, '疫鳞蝰蛇', 2895),
        ], locale='zhCN')
        self.source = self.root / 'managed-talents'
        self.source.mkdir()
        WowTalentVersion.objects.filter(branch='retail').update(is_active=False)
        self.version, _ = WowTalentVersion.objects.update_or_create(
            key='retail', defaults={'branch': 'retail', 'is_active': True,
                                    'current_build': self.name_build, 'source_dir': str(self.source)},
        )
        self.spells = [(173869, 'Twin Fangs', '双牙'), (1308385, 'Visceral Burst', '脏腑爆裂'),
                       (1304459, 'Malignance', '恶念'), (1290779, 'Malice', '恶意'),
                       (1284208, 'Blood Venom', '鲜血毒液'), (1292188, 'Caustic Waves', '腐蚀浪潮')]
        self.spell_tables()
        WowSpellSnapshotState.objects.create(branch='wow', locale='zhCN', snapshot_build='12.1.0.69404')
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', snapshot_build='12.1.0.69404',
                                        spell_id=173869, name='Twin Fangs', name_zh='双生利牙')
        self.service = ArticleTranslationService(engine=object(), glossary=WowNewsGlossary.empty())

    def boss(self, jid, name, did, sections):
        return JournalEncounter.objects.create(instance=self.raid, journal_id=jid, name=name,
                                                payload={'dungeon_encounter_id': did, 'sections': sections})

    def table(self, table, fields, rows, locale='enUS', release_proof=False):
        path = self.directory / (table + ('.enUS' if locale == 'enUS' else '') + '.csv')
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)
        proof = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                 'identity': {'branch': 'wow', 'build': self.relation_build, 'locale': locale, 'table': table},
                 'url': f'https://wago.tools/db2/{table}/csv?build={self.relation_build}&locale={locale}',
                 'rows': len(rows)}
        if release_proof or locale == 'zhCN':
            key = table + ('.enUS' if locale == 'enUS' else '')
            # Historical release manifests have URL/hash, without identity.
            self.release.manifest['tables'][key] = {k: v for k, v in proof.items() if k != 'identity'}
            self.release.save(update_fields=['manifest'])
        else:
            path.with_suffix('.csv.manifest.json').write_text(json.dumps(proof))
        return path

    def spell_tables(self):
        manifest = {}
        for locale, position in [('enUS', 1), ('zhCN', 2)]:
            path = self.source / f'SpellName_{locale}.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['ID', 'Name_lang'])
                rows = self.spells if locale == 'enUS' else list(reversed(self.spells))
                writer.writerows((row[0], row[position]) for row in rows)
            manifest['SpellName' + ('.enUS' if locale == 'enUS' else '')] = {
                'identity': {'branch': 'retail', 'build': self.name_build, 'locale': locale, 'table': 'SpellName'},
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'rows': len(rows),
                'url': f'https://wago.tools/db2/SpellName/csv?build={self.name_build}&locale={locale}',
            }
        (self.source / 'manifest.json').write_text(json.dumps(manifest))

    def glossary(self, text):
        return self.service._glossary_for_article(SimpleNamespace(title='', content=text))

    def restored(self, text):
        glossary = self.glossary(text)
        protected = glossary.protect(text)
        return glossary.restore(protected.text, protected.replacements)

    def test_article_boss_names_override_old_player_spell(self):
        text = 'The Venomous Abyss. Twin Fangs. Vashnik, the Malignant. Ula’tek. Visceral Burst damage reduced. Malignance duration reduced.'
        self.assertEqual(self.restored(text), '烈毒之渊. 双子毒牙. 万毒邪祟者瓦什尼克. 乌拉特克. 脏腑爆裂 damage reduced. 恶念 duration reduced.')
        self.assertEqual(WowSpellSnapshot.objects.get(spell_id=173869).name_zh, '双生利牙')

    def test_scope_id_pairing_references_npc_and_not_typo(self):
        text = 'Ula’tek. Malice damage reduced. Blood Venom duration reduced. Caustic Waves damage reduced. Mechanism Title. Blightscale Viper. Blightsclae Rawling.'
        actual = self.restored(text)
        for term in ['乌拉特克', '恶意', '鲜血毒液', '腐蚀浪潮', '机制标题', '疫鳞蝰蛇', 'Blightsclae Rawling']:
            self.assertIn(term, actual)
        self.assertNotIn('疫鳞幼体', actual)

    def test_plain_malice_not_protected_even_beside_skill_use(self):
        self.service.glossary = WowNewsGlossary.from_trusted_pairs([('Malice', '不应回流的旧译名')])
        text = 'Ula’tek. Their malice was obvious. Malice damage reduced. Ula’tek casts Malice.'
        self.assertEqual(self.restored(text), '乌拉特克. Their malice was obvious. 恶意 damage reduced. 乌拉特克 casts 恶意.')

    def test_no_context_preserves_global_behavior_and_boss_only_scope(self):
        self.assertEqual(self.restored('Twin Fangs damage increased for Rogue.'), '双生利牙 damage increased for Rogue.')
        self.assertEqual(self.restored('Twin Fangs damage increased.'), '双生利牙 damage increased.')
        self.assertEqual(self.restored('Ula’tek. Twin Fangs.'), '乌拉特克. 双子毒牙.')
        text = 'Ordinary news. Malice damage reduced. Blood Venom duration reduced.'
        self.assertEqual(self.restored(text), text)
        self.assertEqual(self.restored('Ula’tek. Malignance damage reduced.'), '乌拉特克. Malignance damage reduced.')
        # No Journal name: keep legacy class/spell behavior, including its
        # snapshot build. The news reader must not change global player data.
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', snapshot_build='12.1.0.69404',
                                        spell_id=999, name='Player Sentinel', name_zh='玩家哨兵')
        self.assertEqual(self.restored('Rogue uses Player Sentinel.'), 'Rogue uses 玩家哨兵.')

    def test_raid_conflict_is_blocked_not_lower_priority_fallback(self):
        self.spells += [(2004, 'Blood Venom', '另一鲜血毒液')]
        self.spell_tables()
        self.ula.payload['sections'].append({'id': 105, 'spell_id': 2004, 'title': '另一鲜血毒液'})
        self.ula.save(update_fields=['payload'])
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', snapshot_build='12.1.0.69404',
                                        spell_id=2004, name='Blood Venom', name_zh='低优先级旧名')
        self.assertEqual(self.restored('The Venomous Abyss. Blood Venom damage reduced.'),
                         '烈毒之渊. Blood Venom damage reduced.')

    def test_missing_current_facts_block_real_lower_priority_names(self):
        self.service.glossary = WowNewsGlossary.from_trusted_pairs([
            ('Malice', '错误恶意'), ('Blood Venom', '错误血毒'),
        ])
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', snapshot_build='12.1.0.69404',
                                        spell_id=999, name='Player Sentinel', name_zh='玩家哨兵')
        path = self.source / 'manifest.json'
        original = path.read_text()
        saved = {locale: (self.source / f'SpellName_{locale}.csv').read_bytes()
                 for locale in ('enUS', 'zhCN')}
        text = 'Ula’tek. Malice damage reduced. Blood Venom duration reduced. Rogue uses Player Sentinel.'
        expected = '乌拉特克. Malice damage reduced. Blood Venom duration reduced. Rogue uses Player Sentinel.'
        for failure in ('bad_hash', 'missing_zh', 'wrong_branch', 'missing_en', 'missing_id'):
            with self.subTest(failure=failure):
                path.write_text(original)
                for locale, content in saved.items():
                    (self.source / f'SpellName_{locale}.csv').write_bytes(content)
                proof = json.loads(original)
                if failure == 'bad_hash':
                    proof['SpellName']['sha256'] = '0' * 64
                elif failure == 'wrong_branch':
                    proof['SpellName']['identity']['branch'] = 'wow'
                elif failure.startswith('missing_') and failure != 'missing_id':
                    locale = 'enUS' if failure == 'missing_en' else 'zhCN'
                    (self.source / f'SpellName_{locale}.csv').unlink()
                else:
                    self.spells = [(sid, en, '' if sid == 1290779 else zh)
                                   for sid, en, zh in self.spells]
                    self.spell_tables()
                    proof = json.loads(path.read_text())
                path.write_text(json.dumps(proof))
                self.assertEqual(self.restored(text), expected if failure != 'missing_id' else
                                 expected.replace('Blood Venom', '鲜血毒液'))
        self.assertEqual(self.restored('Ordinary news. Malice damage reduced.'),
                         'Ordinary news. 错误恶意 damage reduced.')

    def test_missing_english_disables_fallback_for_case_and_other_id_homonyms(self):
        self.service.glossary = WowNewsGlossary.from_trusted_pairs([
            ('Blood Venom', '错误血毒'), ('Malice', '错误恶意'),
        ])
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', snapshot_build='12.1.0.69404',
            spell_id=999999, name='Malice', name_zh='另一旧ID恶意')
        (self.source / 'SpellName_enUS.csv').unlink()
        for text in ('Ula’tek. blood venom duration reduced.',
                     'Ula’tek. Malice damage reduced.'):
            with self.subTest(text=text):
                self.assertEqual(self.restored(text), text.replace('Ula’tek', '乌拉特克'))

    def test_mixed_class_context_blocks_homonymous_boss_only(self):
        from botend.services.news_journal_glossary import read_news_journal_terms
        text = 'The Venomous Abyss. twin fangs. Rogue uses twin fangs.'
        facts = read_news_journal_terms(text)
        self.assertIn('twin fangs', facts.blocked)
        glossary = self.glossary(text)
        self.assertNotIn('twin fangs', glossary._terms_by_key)
        protected = glossary.protect(text)
        self.assertIn('twin fangs. Rogue uses twin fangs.', protected.text)
        self.assertEqual(glossary.restore(protected.text, protected.replacements),
                         '烈毒之渊. twin fangs. Rogue uses twin fangs.')
        for player in ('Rogue', 'Assassination'):
            with self.subTest(player=player):
                text = f'The Venomous Abyss. Twin Fangs. {player} uses Twin Fangs. Ula’tek. Malice damage reduced.'
                self.assertEqual(self.restored(text),
                                 f'烈毒之渊. Twin Fangs. {player} uses Twin Fangs. 乌拉特克. 恶意 damage reduced.')
        self.assertEqual(self.restored('The Venomous Abyss. Twin Fangs. Blood Venom damage reduced.'),
                         '烈毒之渊. 双子毒牙. 鲜血毒液 damage reduced.')
        self.assertEqual(self.restored('The Venomous Abyss. Twin Fangs.'), '烈毒之渊. 双子毒牙.')

    def test_same_translation_keeps_all_identity_evidence(self):
        from botend.services.news_journal_glossary import read_news_journal_terms
        self.spells += [(2004, 'Blood Venom', '鲜血毒液')]
        self.spell_tables()
        self.ula.payload['sections'].append({'id': 105, 'spell_id': 2004, 'title': '鲜血毒液'})
        self.ula.save(update_fields=['payload'])
        facts = read_news_journal_terms('Ula’tek. Blood Venom damage reduced.')
        self.assertEqual({row['id'] for row in facts.evidence['blood venom']}, {1284208, 2004})
        self.assertTrue(all(row['relation_build'] == self.relation_build and row['name_build'] == self.name_build
                            for row in facts.evidence['blood venom']))
        self.assertEqual({row['branch'] for row in facts.evidence['blood venom']}, {'retail'})
        self.assertEqual({row['relation_branch'] for row in facts.evidence['blood venom']}, {'wow'})

    def test_missing_bad_or_tampered_journal_proof_does_not_create_context(self):
        paths = [self.directory / 'DungeonEncounter.enUS.csv', self.directory / 'JournalEncounter.enUS.csv']
        saved = [path.read_bytes() for path in paths]
        for path in paths:
            path.unlink()
        self.assertEqual(self.restored('Ula’tek. Malice damage reduced.'), 'Ula’tek. Malice damage reduced.')
        for path, content in zip(paths, saved):
            path.write_bytes(content)
        path = self.directory / 'JournalInstance.enUS.csv.manifest.json'
        original = json.loads(path.read_text())
        for field, bad in [('build', '12.0.0.1'), ('locale', 'zhCN'), ('branch', 'wowt')]:
            proof = json.loads(json.dumps(original))
            proof['identity'][field] = bad
            path.write_text(json.dumps(proof))
            self.assertNotIn('烈毒之渊', self.restored('The Venomous Abyss. Malice damage reduced.'))
        path.write_text(json.dumps(original))
        csv_path = self.directory / 'JournalInstance.enUS.csv'
        csv_path.write_text(csv_path.read_text().replace('Venomous', 'Counterfeit'))
        self.assertNotIn('烈毒之渊', self.restored('The Counterfeit Abyss. Malice damage reduced.'))

    def test_missing_bad_build_locale_or_hash_spell_pair_fails_closed(self):
        path = self.source / 'manifest.json'
        original = json.loads(path.read_text())
        for field, bad in [('build', '12.1.0.69404'), ('locale', 'enUS'), ('branch', 'wow'), ('branch', 'wowt')]:
            proof = json.loads(json.dumps(original))
            proof['SpellName']['identity'][field] = bad
            path.write_text(json.dumps(proof))
            self.assertEqual(self.restored('Ula’tek. Malice damage reduced.'), '乌拉特克. Malice damage reduced.')
        path.write_text(json.dumps(original))
        spell_path = self.source / 'SpellName_zhCN.csv'
        spell_path.write_text(spell_path.read_text().replace('恶意', '伪造'))
        self.assertEqual(self.restored('Ula’tek. Malice damage reduced.'), '乌拉特克. Malice damage reduced.')
        spell_path.unlink()
        self.assertEqual(self.restored('Ula’tek. Malice damage reduced.'), '乌拉特克. Malice damage reduced.')

    def test_repeated_reads_are_bounded_and_never_network(self):
        from botend.services.news_journal_glossary import read_news_journal_terms
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with patch('socket.socket.connect', side_effect=AssertionError('reader attempted network')):
            first = read_news_journal_terms('Ula’tek. Malice damage reduced.')
            with CaptureQueriesContext(connection) as queries, patch('csv.DictReader', wraps=csv.DictReader) as reader:
                second = read_news_journal_terms('Ula’tek. Malice damage reduced.')
            self.assertEqual(first, second)
            self.assertLessEqual(len(queries), 6)
            self.assertEqual(reader.call_count, 0)

    def test_other_locale_snapshot_style_cannot_override_current_id_pair(self):
        WowSpellSnapshot.objects.create(branch='wow', locale='enUS', spell_id=1290779, name='Malice', name_zh='错误旧名', snapshot_build=self.name_build)
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=1290779, name='恶意', name_zh='', snapshot_build=self.name_build)
        self.assertEqual(self.restored('Ula’tek. Malice damage reduced.'), '乌拉特克. 恶意 damage reduced.')

    def test_source_aliases_are_narrow_not_plural_or_fuzzy(self):
        self.assertEqual(self.restored('Ula’tek. Ula’teks. Vashnik the Malignnt.'), '乌拉特克. Ula’teks. Vashnik the Malignnt.')
        from botend.services.news_journal_glossary import _aliases
        # Another attested Journal name proves this is a punctuation rule,
        # not a per-entity exception (official-journal-name-pairs.json).
        self.assertEqual(_aliases("Nek'zali the Soulcoiler", "Nek’zali, the Soulcoiler. Nek’zali."),
                         {'Nek’zali, the Soulcoiler'})

    def test_ptr_article_never_reuses_retail_journal(self):
        text = 'PTR Ula’tek. Malice damage reduced.'
        self.assertEqual(self.restored(text), text)

    def test_real_tuning_lines_through_html_nodes_without_fuzzy_corrections(self):
        # Exact official-scoped-spell-pairs evidence: names are not selected by
        # the first global English match, nor repaired by edit distance.
        rows = [(1286945, 'Unchecked Rage', '无羁之怒'),
                (1286885, 'Falling Debris', '落石'),
                (1310764, 'Vicious Echoes', '险恶回音')]
        self.spells += rows
        self.spell_tables()
        # Real Journal section identities/titles from
        # lmonitor-article-49951-dbc-20261106/official-journal-name-pairs.json.
        # No compiled payload spell IDs: discovery must follow the raw native
        # encounter relation, not mistake the placeholder titles for spell names.
        self.ula.payload['sections'] = []
        self.ula.save(update_fields=['payload'])
        sections = [(35866, 'Section 22', '第22部分', 1286945),
                    (36739, 'Section 188', '第188部分', 1286885),
                    (37224, 'Section 177', '第177部分', 1310764),
                    (36352, 'Section 97', '第97部分', 1290779)]
        fields = ['ID', 'Title_lang', 'BodyText_lang', 'JournalEncounterID', 'SpellID']
        self.table('JournalEncounterSection', fields,
                   [(sid, en, '', 2895, spell) for sid, en, zh, spell in sections])
        self.table('JournalEncounterSection', fields,
                   [(sid, zh, '', 2895, spell) for sid, en, zh, spell in sections], locale='zhCN')
        source = ('<p><strong>Ula’tek</strong></p><ul>'
                  '<li>Unchecked Rage damage reduced by 61% on all difficulties.</li>'
                  '<li>Falling Debris damage reduced by 46% on Raid Finder.</li>'
                  '<li>Malice damage reduced by 63% on Normal and Heroic difficulty.</li>'
                  '<li>Viscous Echoes impact damage reduced by 53%.</li>'
                  '<li>Blightsclae Rawling</li></ul>')

        class EchoEngine:
            def available(self):
                return True

            def send_message(self, prompt, *, max_tokens):
                # The model isn't this test's subject; round-trip its protected
                # input through the real node translator and restorer.
                return json.dumps(json.loads(prompt.split('输入JSON：\n', 1)[1]))

        from botend.services.article_content_service import html_block_text_nodes
        service = ArticleTranslationService(engine=EchoEngine(), sleep_func=lambda _: None,
                                            glossary=WowNewsGlossary.empty())
        text = '\n'.join(html_block_text_nodes(source))
        glossary = service._glossary_for_article(SimpleNamespace(title='', content=text))
        blocks = [{'type': 'html', 'html': source}]
        actual = service.translate_content_blocks(blocks, glossary=glossary)[0]['html']
        for name in ['乌拉特克', '无羁之怒', '落石', '恶意', 'Viscous Echoes', 'Blightsclae Rawling']:
            self.assertIn(name, actual)
        self.assertNotIn('险恶回音', actual)
        self.assertEqual(len(html_block_text_nodes(actual)), len(html_block_text_nodes(source)))
        self.assertEqual(blocks[0]['html'], source)

    def test_supplemental_or_chinese_source_cannot_cross_id_relations(self):
        # A valid hash is insufficient if a creature row belongs to another boss.
        self.table('JournalEncounterCreature', ['ID', 'Name_lang', 'JournalEncounterID'],
                   [(301, '错误归属', 2887)], locale='zhCN')
        self.assertEqual(self.restored('Ula’tek. Blightscale Viper.'), '乌拉特克. Blightscale Viper.')
        (self.directory / 'JournalEncounterSection.enUS.csv.manifest.json').unlink()
        self.assertEqual(self.restored('Ula’tek. Mechanism Title.'), '乌拉特克. Mechanism Title.')

    def test_source_build_change_invalidates_current_pair_not_journal_identity(self):
        self.version.current_build = '12.1.5.70000'
        self.version.save(update_fields=['current_build'])
        self.assertEqual(self.restored('Ula’tek. Malice damage reduced.'), '乌拉特克. Malice damage reduced.')
