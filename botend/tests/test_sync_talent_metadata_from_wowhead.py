import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from botend.management.commands.sync_talent_metadata_from_wowhead import Command
from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion


class SyncTalentMetadataFromWowheadTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache_dir = Path(self.temporary.name)

    def create_version(self, key, branch):
        return WowTalentVersion.objects.create(
            key=key,
            label=key,
            branch=branch,
            is_active=True,
        )

    def create_node(self, version, *, node_id, spell_id, display_spell_id=None, **values):
        defaults = {
            'talent_version': version,
            'class_name': 'warrior',
            'spec_name': 'arms',
            'tree_type': 'spec',
            'node_id': node_id,
            'spell_id': spell_id,
            'display_spell_id': display_spell_id,
            'name_zh': '旧名称',
            'description_zh': '旧说明x%',
            'icon': 'old_icon',
            'source': 'db2_backfill',
        }
        defaults.update(values)
        return WowTalentNodeMetadata.objects.create(**defaults)

    def write_cache(self, version, data_env, records, locale=4):
        path = Command._cache_path(
            self.cache_dir,
            version_key=version.key,
            data_env=data_env,
            locale=locale,
        )
        payload = {
            'schema_version': 1,
            'version_key': version.key,
            'data_env': data_env,
            'locale': locale,
            'records': {str(key): value for key, value in records.items()},
        }
        Command._write_cache(path, payload)
        return path

    @staticmethod
    def record(*, name='新名称', description='造成10%点伤害。', icon='spell_new'):
        return Command._cache_record(
            status='ok' if description else 'partial',
            name_zh=name,
            description_zh=description,
            icon=icon,
            has_raw_name=bool(name),
            has_raw_description=bool(description),
        )

    def run_sync(self, **overrides):
        options = {
            'cache_dir': str(self.cache_dir),
            'workers': 1,
            'delay': 0,
            'stdout': StringIO(),
        }
        options.update(overrides)
        call_command('sync_talent_metadata_from_wowhead', **options)
        return options['stdout'].getvalue()

    def test_parses_localized_payload_and_rejects_unresolved_placeholder(self):
        valid = Command._record_from_payload({
            'name': '鲜血羁绊',
            'icon': 'Ability DeathKnight Test',
            'tooltip': '<div class="q">恢复10$生命值。<br>持续5秒。</div>',
        })
        invalid = Command._record_from_payload({
            'name': '[Blood Bond]',
            'icon': 'Ability_DeathKnight_Test',
            'tooltip': '<div class="q">恢复x%生命值。</div>',
        })

        self.assertEqual(valid['status'], 'ok')
        self.assertEqual(valid['name_zh'], '鲜血羁绊')
        self.assertEqual(valid['icon'], 'ability-deathknight-test')
        self.assertEqual(valid['description_zh'], '恢复10%生命值。 持续5秒。')
        self.assertEqual(invalid['status'], 'partial')
        self.assertEqual(invalid['name_zh'], '')
        self.assertEqual(invalid['description_zh'], '')
        self.assertEqual(invalid['icon'], 'ability_deathknight_test')

    @mock.patch(
        'botend.management.commands.sync_talent_metadata_from_wowhead.requests.get'
    )
    def test_request_uses_environment_locale_and_project_proxy(self, request_get):
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'name': '鲜血羁绊',
            'icon': 'ability_test',
            'tooltip': '<div class="q">恢复1%生命值。</div>',
        }
        request_get.return_value = response
        proxies = {'https': 'socks5://127.0.0.1:10809'}

        record = Command._fetch_tooltip(
            1267028,
            data_env=3,
            locale=4,
            delay=0,
            proxies=proxies,
        )

        self.assertEqual(record['description_zh'], '恢复1%生命值。')
        self.assertEqual(request_get.call_count, 1)
        self.assertEqual(
            request_get.call_args.kwargs['params'],
            {'dataEnv': 3, 'locale': 4},
        )
        self.assertEqual(request_get.call_args.kwargs['proxies'], proxies)

    def test_updates_duplicate_display_spells_and_preserves_invalid_fields(self):
        version = self.create_version('retail-test', 'retail')
        first = self.create_node(version, node_id=1, spell_id=90, display_spell_id=100)
        second = self.create_node(version, node_id=2, spell_id=91, display_spell_id=100)
        invalid = self.create_node(
            version,
            node_id=3,
            spell_id=200,
            description_zh='人工说明',
            name_zh='人工名称',
            icon='manual_icon',
        )
        self.write_cache(version, 1, {
            100: self.record(),
            200: Command._cache_record(
                status='unlocalized',
                has_raw_name=True,
                has_raw_description=True,
            ),
        })

        output = self.run_sync(version_key=[version.key])

        first.refresh_from_db()
        second.refresh_from_db()
        invalid.refresh_from_db()
        for row in (first, second):
            self.assertEqual(row.name_zh, '新名称')
            self.assertEqual(row.description_zh, '造成10%点伤害。')
            self.assertEqual(row.icon, 'spell_new')
            self.assertEqual(row.source, 'db2+wowhead_live')
        self.assertEqual(
            (invalid.name_zh, invalid.description_zh, invalid.icon, invalid.source),
            ('人工名称', '人工说明', 'manual_icon', 'db2_backfill'),
        )
        self.assertIn('planned_rows=2', output)
        self.assertFalse(WowSpellSnapshot.objects.exists())

    def test_default_run_only_processes_retail_and_leaves_ptr_untouched(self):
        retail = self.create_version('retail-test', 'retail')
        ptr = self.create_version('ptr-test', 'ptr')
        retail_node = self.create_node(retail, node_id=1, spell_id=100)
        ptr_node = self.create_node(ptr, node_id=1, spell_id=100)
        self.write_cache(retail, 1, {
            100: self.record(name='正式服名称', description='正式服说明。'),
        })

        output = self.run_sync()

        retail_node.refresh_from_db()
        ptr_node.refresh_from_db()
        self.assertEqual(retail_node.description_zh, '正式服说明。')
        self.assertEqual(ptr_node.description_zh, '旧说明x%')
        self.assertEqual(retail_node.source, 'db2+wowhead_live')
        self.assertEqual(ptr_node.source, 'db2_backfill')
        self.assertIn('versions=1', output)

    def test_dry_run_reports_changes_without_writing_database(self):
        version = self.create_version('retail-test', 'retail')
        node = self.create_node(version, node_id=1, spell_id=100)
        self.write_cache(version, 1, {100: self.record()})

        output = self.run_sync(version_key=[version.key], dry_run=True)

        node.refresh_from_db()
        self.assertEqual(node.name_zh, '旧名称')
        self.assertEqual(node.description_zh, '旧说明x%')
        self.assertEqual(node.source, 'db2_backfill')
        self.assertIn('DRY RUN', output)
        self.assertIn('dry_run=True', output)

    def test_explicit_ptr_version_is_rejected_without_cache_or_database_writes(self):
        version = self.create_version('ptr-test', 'ptr')
        node = self.create_node(version, node_id=1, spell_id=100)

        with self.assertRaisesMessage(CommandError, '只支持正式服版本'):
            self.run_sync(
                version_key=[version.key],
                dry_run=True,
            )

        node.refresh_from_db()
        self.assertEqual(node.description_zh, '旧说明x%')
        self.assertFalse(
            (self.cache_dir / version.key).exists()
        )

    @mock.patch(
        'botend.management.commands.sync_talent_metadata_from_wowhead.Command._fetch_tooltip',
        return_value=None,
    )
    def test_refresh_failure_keeps_old_cache_and_uses_it_safely(self, fetch_tooltip):
        version = self.create_version('retail-test', 'retail')
        node = self.create_node(version, node_id=1, spell_id=100)
        cache_path = self.write_cache(version, 1, {100: self.record()})
        before = json.loads(cache_path.read_text(encoding='utf-8'))['records']['100']

        output = self.run_sync(
            version_key=[version.key],
            refresh=True,
        )

        node.refresh_from_db()
        after = json.loads(cache_path.read_text(encoding='utf-8'))['records']['100']
        self.assertEqual(after, before)
        self.assertEqual(node.description_zh, '造成10%点伤害。')
        self.assertEqual(fetch_tooltip.call_count, 1)
        self.assertIn('request_failed=1', output)
