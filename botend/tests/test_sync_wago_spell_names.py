import csv
import tempfile
from io import StringIO
from pathlib import Path

from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from botend.management.commands.sync_wago_spell_names import Command
from botend.models import WowSpellSnapshot, WowSpellSnapshotState


class SyncWagoSpellNamesTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dump_dir = Path(self.tmp.name)

    def write_names(self, locale, rows):
        path = self.dump_dir / f'SpellName_{locale}.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['ID', 'Name_lang'])
            writer.writeheader()
            writer.writerows({'ID': spell_id, 'Name_lang': name} for spell_id, name in rows)
        return path

    def run_sync(self, **overrides):
        options = {
            'dump_dir': str(self.dump_dir),
            'build': '12.0.7.68453',
            'min_paired': 1,
            'stdout': StringIO(),
        }
        options.update(overrides)
        call_command('sync_wago_spell_names', **options)
        return options['stdout'].getvalue()

    def test_pairs_locales_by_spell_id_and_writes_only_complete_pairs(self):
        self.write_names('enUS', [(100, 'Bloodthirst'), (200, 'Rampage')])
        self.write_names('zhCN', [(200, '暴怒'), (300, '斩杀'), (100, '嗜血')])

        self.run_sync()

        rows = list(WowSpellSnapshot.objects.order_by('spell_id').values(
            'branch', 'locale', 'spell_id', 'name', 'name_zh', 'snapshot_build'))
        self.assertEqual(rows, [
            {'branch': 'wow', 'locale': 'zhCN', 'spell_id': 100,
             'name': 'Bloodthirst', 'name_zh': '嗜血', 'snapshot_build': '12.0.7.68453'},
            {'branch': 'wow', 'locale': 'zhCN', 'spell_id': 200,
             'name': 'Rampage', 'name_zh': '暴怒', 'snapshot_build': '12.0.7.68453'},
        ])
        self.assertEqual(
            WowSpellSnapshotState.objects.get(branch='wow', locale='zhCN').snapshot_build,
            '12.0.7.68453',
        )

    def test_is_idempotent_and_preserves_description_fields(self):
        row = WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=100,
            name='Old', name_zh='旧名', description='description',
            aura_description='aura', snapshot_build='old-build',
        )
        self.write_names('enUS', [(100, 'Bloodthirst')])
        self.write_names('zhCN', [(100, '嗜血')])

        first = self.run_sync()
        row.refresh_from_db()
        first_updated_at = row.updated_at
        second = self.run_sync()
        row.refresh_from_db()

        self.assertIn('updated=1', first)
        self.assertIn('unchanged=1', second)
        self.assertEqual(row.name, 'Bloodthirst')
        self.assertEqual(row.name_zh, '嗜血')
        self.assertEqual(row.description, 'description')
        self.assertEqual(row.aura_description, 'aura')
        self.assertEqual(row.updated_at, first_updated_at)

    def test_sync_does_not_update_unpaired_existing_rows(self):
        paired = WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=100,
            name='Old', name_zh='旧名', snapshot_build='old-build')
        unpaired = WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=999,
            name='Keep', name_zh='保留', snapshot_build='old-build')
        self.write_names('enUS', [(100, 'Bloodthirst')])
        self.write_names('zhCN', [(100, '嗜血')])

        self.run_sync()

        paired.refresh_from_db()
        unpaired.refresh_from_db()
        self.assertEqual((paired.name, paired.name_zh), ('Bloodthirst', '嗜血'))
        self.assertEqual(
            (unpaired.name, unpaired.name_zh, unpaired.snapshot_build),
            ('Keep', '保留', 'old-build'))

    def test_rejects_non_live_branch_and_latest_build_without_writes(self):
        self.write_names('enUS', [(100, 'Bloodthirst')])
        self.write_names('zhCN', [(100, '嗜血')])

        for kwargs, message in (({'branch': 'wowt'}, 'branch'), ({'build': 'latest'}, 'build')):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CommandError, message):
                self.run_sync(**kwargs)

        self.assertFalse(WowSpellSnapshot.objects.exists())
        self.assertFalse(WowSpellSnapshotState.objects.exists())

    def test_rejects_malformed_or_incomplete_dump_without_advancing_state(self):
        WowSpellSnapshotState.objects.create(
            branch='wow', locale='zhCN', snapshot_build='old-build')
        self.write_names('enUS', [(100, 'Bloodthirst')])
        (self.dump_dir / 'SpellName_zhCN.csv').write_text('<html>error</html>', encoding='utf-8')

        with self.assertRaises(CommandError):
            self.run_sync()

        self.assertEqual(
            WowSpellSnapshotState.objects.get(branch='wow', locale='zhCN').snapshot_build,
            'old-build',
        )
        self.assertFalse(WowSpellSnapshot.objects.exists())

    def test_dry_run_reports_changes_without_writing_or_advancing_state(self):
        WowSpellSnapshotState.objects.create(
            branch='wow', locale='zhCN', snapshot_build='old-build')
        self.write_names('enUS', [(100, 'Bloodthirst')])
        self.write_names('zhCN', [(100, '嗜血')])

        output = self.run_sync(dry_run=True)

        self.assertIn('created=1', output)
        self.assertIn('dry_run=True', output)
        self.assertFalse(WowSpellSnapshot.objects.exists())
        self.assertEqual(
            WowSpellSnapshotState.objects.get(branch='wow', locale='zhCN').snapshot_build,
            'old-build',
        )

    def test_mysql_load_rows_match_the_snapshot_model_columns(self):
        row = WowSpellSnapshot(
            branch='wow', locale='zhCN', spell_id=100,
            name='Bloodthirst', name_zh='嗜血', snapshot_build='12.0.7.68453',
        )

        columns, values = Command._mysql_load_row(row)

        self.assertEqual(columns, (
            'branch', 'locale', 'spell_id', 'name', 'name_zh',
            'description', 'aura_description', 'snapshot_build', 'updated_at',
        ))
        self.assertEqual(values[:8], (
            'wow', 'zhCN', 100, 'Bloodthirst', '嗜血', '', '', '12.0.7.68453',
        ))
        self.assertIsNotNone(values[8])

    @mock.patch('botend.management.commands.sync_wago_spell_names.requests.get')
    def test_downloads_both_locales_for_the_explicit_build(self, get):
        def response_for(url, timeout):
            locale = 'zhCN' if 'locale=zhCN' in url else 'enUS'
            name = '嗜血' if locale == 'zhCN' else 'Bloodthirst'
            payload = f'ID,Name_lang\r\n100,{name}\r\n'.encode()
            response = mock.Mock(content=payload, headers={'Content-Type': 'text/csv; charset=UTF-8'})
            response.raise_for_status.return_value = None
            return response

        get.side_effect = response_for
        output = StringIO()
        call_command(
            'sync_wago_spell_names', build='12.0.7.68453', min_paired=1,
            stdout=output,
        )

        self.assertEqual(get.call_count, 2)
        requested_urls = [call.args[0] for call in get.call_args_list]
        self.assertTrue(all('build=12.0.7.68453' in url for url in requested_urls))
        self.assertTrue(any('locale=enUS' in url for url in requested_urls))
        self.assertTrue(any('locale=zhCN' in url for url in requested_urls))
        self.assertEqual(WowSpellSnapshot.objects.get(spell_id=100).name_zh, '嗜血')
