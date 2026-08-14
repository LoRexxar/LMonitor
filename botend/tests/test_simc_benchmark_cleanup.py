import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import TestCase

from botend.management.commands.cleanup_simc_benchmark_history import BACKUP_SCHEMA, Command
from botend.models import (
    SimcBackendBinary,
    SimcBenchmarkCase,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcBenchmarkResult,
    SimcTask,
    SimcTaskArtifact,
    SimulationRun,
)


class _Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeOss:
    HeadObjectRequest = _Request
    GetObjectAclRequest = _Request
    PutObjectAclRequest = _Request
    CopyObjectRequest = _Request
    DeleteMultipleObjectsRequest = _Request
    DeleteObject = _Request


class _NotFound(Exception):
    status_code = 404
    code = 'NoSuchKey'


class _FakeOssClient:
    def __init__(self, objects, *, corrupt_copy=False):
        self.objects = objects
        self.corrupt_copy = corrupt_copy

    def head_object(self, request):
        try:
            value = self.objects[request.key]
        except KeyError as exc:
            raise _NotFound() from exc
        return SimpleNamespace(**{key: value.get(key) for key in (
            'content_length', 'content_type', 'etag', 'content_md5', 'metadata',
            'cache_control', 'content_disposition', 'content_encoding', 'expires',
            'hash_crc64', 'storage_class', 'server_side_encryption',
            'server_side_data_encryption', 'server_side_encryption_key_id',
            'last_modified', 'version_id',
        )})

    def get_object_acl(self, request):
        try:
            return SimpleNamespace(acl=self.objects[request.key]['acl'])
        except KeyError as exc:
            raise _NotFound() from exc

    def put_object_acl(self, request):
        self.objects[request.key]['acl'] = request.acl

    def copy_object(self, request):
        expected_etag = getattr(request, 'if_match', None)
        if expected_etag and expected_etag != self.objects[request.source_key]['etag']:
            raise RuntimeError('precondition failed')
        source = dict(self.objects[request.source_key])
        source['metadata'] = dict(source.get('metadata') or {})
        source['acl'] = getattr(request, 'acl', None) or 'private'
        if self.corrupt_copy:
            source['metadata']['sha256'] = 'f' * 64
        self.objects[request.key] = source

    def delete_multiple_objects(self, request):
        deleted = []
        for item in request.objects:
            if item.key in self.objects:
                del self.objects[item.key]
            deleted.append(SimpleNamespace(key=item.key))
        return SimpleNamespace(deleted_objects=deleted)


class SimcBenchmarkCleanupCommandTests(TestCase):
    def _source_state(self):
        return {
            'content_length': 5,
            'content_type': 'text/html; charset=utf-8',
            'etag': '"etag-value"',
            'content_md5': 'md5-value',
            'metadata': {'sha256': 'a' * 64, 'lease-fence': 'lease'},
            'cache_control': 'no-cache',
            'content_disposition': None,
            'content_encoding': None,
            'expires': None,
            'hash_crc64': '12345',
            'storage_class': 'Standard',
            'server_side_encryption': None,
            'server_side_data_encryption': None,
            'server_side_encryption_key_id': None,
            'last_modified': '2026-08-01T00:00:00+00:00',
            'version_id': None,
            'acl': 'public-read',
        }

    def test_oss_quarantine_preserves_full_identity_and_delete_verifies_original_is_gone(self):
        source_key = 'simc_agent_results/simc_task_1_run_1.html'
        objects = {source_key: self._source_state()}
        client = _FakeOssClient(objects)
        command = Command()

        with patch(
            'botend.management.commands.cleanup_simc_benchmark_history._client',
            return_value=(_FakeOss, client, 'bucket'),
        ):
            quarantine_map = command._quarantine_objects((source_key,), '20260814T010203Z-abcdef123456')
            descriptor = quarantine_map[source_key]
            target_key = descriptor['quarantine_key']
            self.assertEqual(descriptor['source']['etag'], '"etag-value"')
            self.assertEqual(descriptor['source']['metadata'], self._source_state()['metadata'])
            self.assertEqual(descriptor['source']['acl'], 'public-read')
            self.assertEqual(objects[target_key]['acl'], 'public-read')

            command._delete_objects(quarantine_map)

        self.assertNotIn(source_key, objects)
        self.assertIn(target_key, objects)

    def test_oss_quarantine_mismatch_never_deletes_original(self):
        source_key = 'simc_agent_results/simc_task_1_run_1.html'
        objects = {source_key: self._source_state()}
        client = _FakeOssClient(objects, corrupt_copy=True)
        command = Command()

        with patch(
            'botend.management.commands.cleanup_simc_benchmark_history._client',
            return_value=(_FakeOss, client, 'bucket'),
        ), self.assertRaises(CommandError):
            command._quarantine_objects((source_key,), '20260814T010203Z-abcdef123456')

        self.assertIn(source_key, objects)

    def test_oss_quarantine_binds_dry_run_age_identity_and_uses_conditional_copy(self):
        source_key = 'simc_agent_results/simc_task_1_run_1.html'
        objects = {source_key: self._source_state()}
        client = _FakeOssClient(objects)
        command = Command()
        copied = []
        original_copy = client.copy_object

        def capture_copy(request):
            copied.append(request)
            return original_copy(request)

        client.copy_object = capture_copy
        with patch(
            'botend.management.commands.cleanup_simc_benchmark_history._client',
            return_value=(_FakeOss, client, 'bucket'),
        ):
            command._quarantine_objects(
                (source_key,), '20260814T010203Z-abcdef123456',
                expected_states={source_key: {
                    'content_length': 5,
                    'last_modified': '2026-08-01T00:00:00+00:00',
                }},
            )

        self.assertEqual(copied[0].if_match, '"etag-value"')

        replaced = self._source_state()
        replaced['last_modified'] = '2026-08-14T00:00:00+00:00'
        objects = {source_key: replaced}
        with patch(
            'botend.management.commands.cleanup_simc_benchmark_history._client',
            return_value=(_FakeOss, _FakeOssClient(objects), 'bucket'),
        ), self.assertRaises(CommandError):
            command._quarantine_objects(
                (source_key,), '20260814T010203Z-fedcba654321',
                expected_states={source_key: {
                    'content_length': 5,
                    'last_modified': '2026-08-01T00:00:00+00:00',
                }},
            )

        self.assertEqual(set(objects), {source_key})

    def test_locked_validation_requires_the_global_simc_queue_to_be_drained(self):
        backend = SimcBackendBinary.objects.create(
            identifier='cleanup-in-flight', name='Cleanup', current_version='b' * 40,
        )
        SimcTask.objects.create(
            user_id=1, name='unrelated pending task', simc_profile_id=1,
            backend=backend, current_status=0,
        )

        with self.assertRaises(CommandError):
            Command()._validate_locked_plan(SimpleNamespace(
                warnings=(), deletable_task_ids=frozenset(),
            ))

    def test_database_cleanup_removes_only_artifact_index_and_keeps_all_history_records(self):
        backend = SimcBackendBinary.objects.create(
            identifier='cleanup-oss-only', name='Cleanup', current_version='c' * 40,
        )
        panel = SimcBenchmarkPanel.objects.create(
            name='Cleanup', slug='cleanup-oss-only', created_by_id=1,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='d' * 64,
            status=SimcBenchmarkExecution.STATUS_SUCCESS,
        )
        task = SimcTask.objects.create(
            user_id=1, name='historical benchmark', simc_profile_id=1,
            backend=backend, mode='comparison', current_status=2,
        )
        run = SimulationRun.objects.create(
            task=task, sequence=1, status='completed', result_summary={'dps': 100},
        )
        artifact = SimcTaskArtifact.objects.create(
            task=task, run=run, artifact_type='html_report',
            file_path='simc_agent_results/simc_task_1_run_1.html', file_size=5,
        )
        case = SimcBenchmarkCase.objects.create(
            execution=execution, task=task, status=SimcBenchmarkExecution.STATUS_SUCCESS,
            spec_key='mage_frost', scenario_key='single', profile_key='default',
            spec_label='冰法', scenario_label='单体', profile_label='默认',
            coordinate_hash='e' * 64,
        )
        result = SimcBenchmarkResult.objects.create(
            case=case, candidate_key='baseline', dps=100,
        )
        plan = SimpleNamespace(
            result_ids=frozenset({result.id}),
            deletable_case_ids=frozenset({case.id}),
            artifact_ids=frozenset({artifact.id}),
            run_ids=frozenset({run.id}),
            deletable_task_ids=frozenset({task.id}),
            deletable_execution_ids=frozenset({execution.id}),
        )

        Command()._delete_artifact_rows(plan)

        self.assertFalse(SimcTaskArtifact.objects.filter(pk=artifact.id).exists())
        self.assertTrue(SimcBenchmarkExecution.objects.filter(pk=execution.id).exists())
        self.assertTrue(SimcBenchmarkCase.objects.filter(pk=case.id).exists())
        self.assertTrue(SimcTask.objects.filter(pk=task.id).exists())
        self.assertTrue(SimulationRun.objects.filter(pk=run.id).exists())
        self.assertTrue(SimcBenchmarkResult.objects.filter(pk=result.id).exists())

    def test_signed_backup_restores_artifact_idempotently_and_rejects_conflicts(self):
        backend = SimcBackendBinary.objects.create(
            identifier='cleanup-backup', name='Cleanup', current_version='a' * 40,
        )
        task = SimcTask.objects.create(
            user_id=1, name='historical', simc_profile_id=1, backend=backend,
            mode='comparison', current_status=2,
        )
        run = SimulationRun.objects.create(
            task=task, sequence=1, status='completed', result_summary={'dps': 100},
        )
        artifact = SimcTaskArtifact.objects.create(
            task=task, run=run, artifact_type='html_report',
            file_path='simc_agent_results/simc_task_1_run_1.html', file_size=5,
        )
        artifact_id = artifact.id
        command = Command()
        backup = {
            'backup_schema': BACKUP_SCHEMA,
            'plan': {'fingerprint': 'fingerprint', 'batch_id': '20260814T010203Z-abcdef123456'},
            'quarantine_map': {},
            'object_keys': [],
            'records': {
                'artifacts': command._serialize(
                    SimcTaskArtifact.objects.filter(pk=artifact.id)
                ),
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            backup_path = Path(tmp) / 'cleanup.json.gz'
            with patch.object(command, '_read_backup', wraps=command._read_backup) as verify:
                command._write_backup(backup_path, backup)
            verify.assert_called_once_with(backup_path)
            artifact.delete()

            command._rollback(backup_path)
            restored = SimcTaskArtifact.objects.get(pk=artifact_id)
            self.assertEqual(restored.task_id, task.id)
            self.assertEqual(restored.run_id, run.id)
            self.assertTrue(SimcTask.objects.filter(pk=task.id).exists())
            self.assertTrue(SimulationRun.objects.filter(pk=run.id).exists())

            command._rollback(backup_path)
            SimcTaskArtifact.objects.filter(pk=artifact_id).update(file_size=6)
            with self.assertRaises(CommandError):
                command._rollback(backup_path)

            with gzip.open(backup_path, 'rt', encoding='utf-8') as stream:
                tampered = json.load(stream)
            tampered['plan']['fingerprint'] = 'tampered'
            tampered_path = Path(tmp) / 'tampered.json.gz'
            with gzip.open(tampered_path, 'wt', encoding='utf-8') as stream:
                json.dump(tampered, stream)
            with self.assertRaises(CommandError):
                command._rollback(tampered_path)
