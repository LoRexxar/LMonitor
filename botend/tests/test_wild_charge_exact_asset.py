"""Real resolver regression for the bundled retail Wild Charge closure."""
import csv
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile

from django.test import TestCase

from botend.wow.spell_text import SpellTextResolver


class WildChargeExactAssetTests(TestCase):
    def test_exact_asset_resolves_all_forms_and_numeric_dependencies(self):
        asset = Path(__file__).resolve().parents[1] / 'data/retail_talent_db2_12.1.0.69283.tar.gz'
        with tarfile.open(asset) as archive, tempfile.TemporaryDirectory() as temporary:
            manifest = json.load(archive.extractfile('manifest.json'))
            self.assertEqual(manifest['build'], '12.1.0.69283')
            self.assertEqual(manifest['version_key'], 'retail')
            self.assertEqual(len(manifest['files']), 15)
            self.assertEqual(set(archive.getnames()), set(manifest['files']) | {'manifest.json'})
            for filename, expected in manifest['files'].items():
                payload = archive.extractfile(filename).read()
                self.assertEqual(len(payload), expected['bytes'], filename)
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected['sha256'], filename)
                (Path(temporary) / filename).write_bytes(payload)
            for locale in ('enUS', 'zhCN'):
                with self.subTest(locale=locale):
                    definitions = csv.DictReader(io.StringIO(archive.extractfile(f'TraitDefinition_{locale}.csv').read().decode()))
                    definition = next(row for row in definitions if row['ID'] == '108281')
                    self.assertEqual(definition['SpellID'], '102401')
                    self.assertEqual(definition['OverrideDescription_lang'], '$@spelldesc132302')
                    resolver = SpellTextResolver(locale=locale, branch='retail', snapshot_build=manifest['build'], dump_dir=temporary)
                    text = resolver.resolve(definition['OverrideDescription_lang'], 102401, known_spell_ids={24858})
                    self.assertTrue(text)
                    self.assertNotRegex(text, r'\$|\?[as]\d|\|[CcRr]')
                    if locale == 'enUS':
                        for fragment in ('Non-shapeshifted', "Fly to an ally's position", 'Bear Form', 'immobilizing them for 4', 'Cat Form', 'dazing them for 3', 'Moonkin Form', 'Bound backward', 'Travel Form', '20 yds', 'Aquatic Form', '150% for 5'):
                            self.assertIn(fragment, text)
                    else:
                        for fragment in ('人形态', '飞向盟友的位置', '熊形态', '无法移动，持续4', '猎豹形态', '眩晕3', '枭兽形态', '向后弹跳', '旅行形态', '20码', '水栖形态', '150%，持续5'):
                            self.assertIn(fragment, text)
                    self.assertEqual(resolver._duration_value(45334, 1), '4')
                    self.assertEqual(resolver._duration_value(50259, 1), '3')
                    self.assertEqual(resolver._duration_value(102416, 1), '5')
                    self.assertEqual(resolver._spell_name(132302), '野性冲锋' if locale == 'zhCN' else 'Wild Charge')
                    without_moonkin = resolver.resolve(definition['OverrideDescription_lang'], 102401, known_spell_ids=set())
                    self.assertNotIn('枭兽形态' if locale == 'zhCN' else 'Moonkin Form', without_moonkin)
