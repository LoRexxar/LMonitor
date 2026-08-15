from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import (
    SimcBenchmarkCase,
    SimcBenchmarkCandidate,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkResult,
    SimcBenchmarkScenario,
    SimcBenchmarkSpec,
    SimcTask,
    SimcTaskArtifact,
    SimcTaskFavorite,
    SimulationRun,
)
from botend.services.simc_agent_oss import _client
from botend.services.simc_benchmark_cleanup import (
    AGENT_OBJECT_RE,
    ROOT_ATTRIBUTE_RE,
    ROOT_RUN_RE,
    ROOT_TASK_RE,
    ROOT_UUID_RE,
    _artifact_object_key,
    build_cleanup_plan,
    list_oss_orphan_reports,
)


BACKUP_SCHEMA = 3
PLAN_SCHEMA = 3
QUARANTINE_PREFIX = 'simc_cleanup_quarantine/'
BATCH_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{7,95}$')
OBJECT_STATE_FIELDS = (
    'content_length',
    'content_type',
    'etag',
    'content_md5',
    'metadata',
    'cache_control',
    'content_disposition',
    'content_encoding',
    'expires',
    'hash_crc64',
    'storage_class',
    'server_side_encryption',
    'server_side_data_encryption',
    'server_side_encryption_key_id',
    'last_modified',
    'version_id',
    'acl',
)
OBJECT_IDENTITY_FIELDS = tuple(
    name for name in OBJECT_STATE_FIELDS if name not in {'last_modified', 'version_id'}
)
RECORD_MODELS = {'artifacts': SimcTaskArtifact}
RECORD_ORDER = tuple(RECORD_MODELS)


class Command(BaseCommand):
    help = (
        '清理已被当前 Benchmark 投影覆盖的历史 OSS 报告及其 Artifact 索引；'
        'Execution/Case/Task/Run/Result 全部保留。'
        '默认只读 dry-run；apply 前必须用本次 fingerprint 显式确认。'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='执行隔离、Artifact 索引删除及 OSS 原键删除；不删除运行历史。',
        )
        parser.add_argument('--confirm-fingerprint', default='', help='必须与本次 dry-run fingerprint 完全一致。')
        parser.add_argument('--minimum-age-days', type=int, default=7, help='纯 OSS 孤儿的最小年龄，默认 7 天。')
        parser.add_argument('--skip-oss-orphans', action='store_true', help='不枚举或清理纯 OSS 孤儿。')
        parser.add_argument('--write-plan', default='', help='将 dry-run 计划写入指定 JSON 文件。')
        parser.add_argument('--backup', default='', help='apply 的可恢复 gzip JSON 备份路径。')
        parser.add_argument('--rollback', default='', help='从指定 gzip JSON 备份恢复数据库与 OSS 原键。')

    def handle(self, *args, **options):
        if options['rollback']:
            if options['apply']:
                raise CommandError('--rollback 与 --apply 不能同时使用')
            return self._rollback(Path(options['rollback']))

        minimum_age_days = options['minimum_age_days']
        if minimum_age_days < 1:
            raise CommandError('--minimum-age-days 必须至少为 1')
        include_orphans = not options['skip_oss_orphans']
        plan, orphans, document = self._build_document(
            minimum_age_days=minimum_age_days,
            include_orphans=include_orphans,
        )
        self._print_document(document, mode='APPLY-CANDIDATE' if options['apply'] else 'DRY-RUN')

        if options['write_plan']:
            self._write_json(Path(options['write_plan']), document)
            self.stdout.write(f'plan={Path(options["write_plan"]).resolve()}')
        if not options['apply']:
            return
        if plan.warnings:
            raise CommandError('存在规划告警，拒绝 apply')
        if options['confirm_fingerprint'] != document['fingerprint']:
            raise CommandError('confirm fingerprint 不匹配；请先重新 dry-run')
        self._assert_simc_queue_drained()

        backup_path = Path(options['backup']) if options['backup'] else self._default_backup_path(document)
        if backup_path.exists():
            raise CommandError(f'备份路径已存在，拒绝覆盖: {backup_path}')
        object_keys = tuple(document['object_keys'])
        quarantine_map = self._quarantine_objects(
            object_keys,
            document['batch_id'],
            expected_states=document['oss_orphan_expectations'],
        )
        backup_written = False

        try:
            with transaction.atomic():
                self._lock_cleanup_rows()
                locked_plan = build_cleanup_plan()
                if locked_plan.fingerprint != plan.fingerprint:
                    raise CommandError('事务锁定后数据库计划变化，拒绝删除')
                self._validate_locked_plan(locked_plan)
                self._validate_orphans_unreferenced(orphans)

                backup = self._build_backup(locked_plan, document, object_keys)
                backup['quarantine_map'] = quarantine_map
                self._write_backup(backup_path, backup)
                backup_written = True
                self.stdout.write(f'backup={backup_path.resolve()}')

                # All Task and Artifact rows remain locked until the transaction
                # commits, so an old pure-OSS orphan cannot gain a new DB owner
                # between this check and deletion.
                self._delete_objects(quarantine_map)
                self._delete_artifact_rows(locked_plan)
        except Exception:
            # A normal Python/DB failure must not leave registered report keys
            # missing while the DB transaction rolls back. Process death is
            # recovered with the already-written signed backup.
            if quarantine_map:
                try:
                    self._restore_objects(quarantine_map)
                except Exception as restore_exc:
                    suffix = f'; OSS 自动恢复亦失败: {restore_exc}'
                    if backup_written:
                        suffix += f'; 请立即执行 --rollback {backup_path.resolve()}'
                    raise CommandError('清理失败' + suffix) from restore_exc
            raise

        self.stdout.write(self.style.SUCCESS(
            f'cleanup complete fingerprint={document["fingerprint"]} '
            f'db_artifacts={len(plan.artifact_ids)} oss_objects={len(quarantine_map)} '
            'history_records_retained=true '
            f'backup={backup_path.resolve()}'
        ))

    def _build_document(self, *, minimum_age_days, include_orphans):
        plan = build_cleanup_plan()
        orphans = (
            list_oss_orphan_reports(minimum_age_days=minimum_age_days)
            if include_orphans else ()
        )
        object_keys = tuple(sorted(set(plan.object_keys) | {row.key for row in orphans}))
        for key in object_keys:
            self._validate_report_key(key)
        fingerprint_payload = {
            'database': plan.fingerprint,
            'orphans': [
                [row.key, row.size, row.last_modified, row.reason]
                for row in orphans
            ],
        }
        fingerprint = hashlib.sha256(self._canonical_json(fingerprint_payload)).hexdigest()
        batch_id = 'cleanup-' + fingerprint[:32]
        planner_summary = plan.summary()
        summary = {
            'protected_tasks': planner_summary['protected_tasks'],
            'historical_tasks': planner_summary['deletable_tasks'],
            'retained_cases': planner_summary['deletable_cases'],
            'retained_executions': planner_summary['deletable_executions'],
            'retained_runs': planner_summary['deletable_runs'],
            'delete_artifacts': planner_summary['deletable_artifacts'],
            'retained_results': planner_summary['deletable_results'],
            'registered_report_bytes': planner_summary['report_bytes'],
            'warnings': planner_summary['warnings'],
            'oss_orphans': len(orphans),
            'oss_orphan_bytes': sum(row.size for row in orphans),
            'oss_total_objects': len(object_keys),
            'oss_total_bytes_upper_bound': plan.report_bytes + sum(row.size for row in orphans),
        }
        document = {
            'schema': PLAN_SCHEMA,
            'created_at': timezone.now().isoformat(),
            'batch_id': batch_id,
            'fingerprint': fingerprint,
            'database_fingerprint': plan.fingerprint,
            'minimum_age_days': minimum_age_days,
            'include_oss_orphans': include_orphans,
            'summary': summary,
            'ids': {
                'retained_history': {
                    'tasks': sorted(plan.deletable_task_ids),
                    'cases': sorted(plan.deletable_case_ids),
                    'executions': sorted(plan.deletable_execution_ids),
                    'runs': sorted(plan.run_ids),
                    'results': sorted(plan.result_ids),
                },
                'delete': {
                    'artifacts': sorted(plan.artifact_ids),
                },
            },
            'database_state': plan.state_manifest(),
            'object_keys': list(object_keys),
            'oss_orphans': [row.__dict__ for row in orphans],
            'oss_orphan_expectations': {
                row.key: {
                    'content_length': row.size,
                    'last_modified': row.last_modified,
                }
                for row in orphans
            },
        }
        return plan, orphans, document

    def _print_document(self, document, *, mode):
        summary = document['summary']
        self.stdout.write(
            f'{mode} fingerprint={document["fingerprint"]} '
            f'protected_tasks={summary["protected_tasks"]} '
            f'historical_tasks={summary["historical_tasks"]} '
            f'retained_cases={summary["retained_cases"]} '
            f'retained_executions={summary["retained_executions"]} '
            f'retained_runs={summary["retained_runs"]} '
            f'delete_artifacts={summary["delete_artifacts"]} '
            f'retained_results={summary["retained_results"]}'
        )
        self.stdout.write(
            f'OSS registered_bytes={summary["registered_report_bytes"]} '
            f'orphan_objects={summary["oss_orphans"]} orphan_bytes={summary["oss_orphan_bytes"]} '
            f'total_objects={summary["oss_total_objects"]} '
            f'upper_bound_bytes={summary["oss_total_bytes_upper_bound"]}'
        )
        for warning in summary['warnings']:
            self.stdout.write(self.style.WARNING(f'WARNING {warning}'))

    @staticmethod
    def _canonical_json(value):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        ).encode('utf-8')

    @staticmethod
    def _default_backup_path(document):
        return Path(settings.BASE_DIR) / 'var' / 'backups' / (
            f'simc_benchmark_report_cleanup_{document["batch_id"]}.json.gz'
        )

    @staticmethod
    def _write_json(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    @staticmethod
    def _serialize(queryset):
        return json.loads(serializers.serialize('json', queryset))

    def _build_backup(self, plan, document, object_keys):
        return {
            'backup_schema': BACKUP_SCHEMA,
            'created_at': timezone.now().isoformat(),
            'plan': document,
            'quarantine_map': {},
            'object_keys': list(object_keys),
            'records': {
                'artifacts': self._serialize(
                    SimcTaskArtifact.objects.filter(id__in=plan.artifact_ids).order_by('id')
                ),
            },
        }

    def _backup_signature(self, payload):
        key = str(settings.SECRET_KEY).encode('utf-8')
        return hmac.new(key, self._canonical_json(payload), hashlib.sha256).hexdigest()

    def _write_backup(self, path, data):
        payload = deepcopy(data)
        payload.pop('integrity', None)
        payload['record_counts'] = {
            group: len(payload.get('records', {}).get(group, ()))
            for group in RECORD_ORDER
        }
        signed = deepcopy(payload)
        signed['integrity'] = {
            'algorithm': 'hmac-sha256',
            'digest': self._backup_signature(payload),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        encoded = json.dumps(
            signed, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8')
        with open(temporary, 'wb') as raw_stream:
            with gzip.GzipFile(fileobj=raw_stream, mode='wb') as stream:
                stream.write(encoded)
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        # Destructive work starts only after the durable file can be parsed,
        # authenticated, schema-validated, and record-count validated end-to-end.
        self._read_backup(path)

    def _read_backup(self, path):
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                signed = json.load(stream)
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError(f'备份无法读取: {path}') from exc
        integrity = signed.pop('integrity', None)
        expected = self._backup_signature(signed)
        if not isinstance(integrity, dict) or integrity.get('algorithm') != 'hmac-sha256':
            raise CommandError('备份缺少受支持的完整性认证')
        if not hmac.compare_digest(str(integrity.get('digest') or ''), expected):
            raise CommandError('备份完整性认证失败')
        self._validate_backup(signed)
        return signed

    @staticmethod
    def _is_not_found(exc):
        errors = [exc]
        unwrap = getattr(exc, 'unwrap', None)
        if callable(unwrap):
            errors.append(unwrap())
        return any(
            error is not None and (
                getattr(error, 'status_code', None) == 404
                or str(getattr(error, 'code', '') or getattr(error, 'error_code', ''))
                in {'NoSuchKey', 'NotFound', 'NoSuchObject'}
            )
            for error in errors
        )

    def _head_object(self, oss, client, bucket, key):
        try:
            return client.head_object(oss.HeadObjectRequest(bucket=bucket, key=key))
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise

    def _object_snapshot(self, oss, client, bucket, key):
        head = self._head_object(oss, client, bucket, key)
        if head is None:
            return None
        try:
            acl = client.get_object_acl(oss.GetObjectAclRequest(bucket=bucket, key=key)).acl
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise
        metadata = {
            str(name).lower(): str(value)
            for name, value in (getattr(head, 'metadata', None) or {}).items()
        }
        snapshot = {
            name: getattr(head, name, None)
            for name in OBJECT_STATE_FIELDS
            if name not in {'metadata', 'acl'}
        }
        snapshot['content_length'] = int(snapshot['content_length'] or 0)
        snapshot['etag'] = str(snapshot['etag'] or '')
        modified = getattr(head, 'last_modified', None)
        snapshot['last_modified'] = (
            modified.isoformat() if hasattr(modified, 'isoformat')
            else (str(modified) if modified is not None else None)
        )
        snapshot['version_id'] = str(getattr(head, 'version_id', None) or '') or None
        snapshot['metadata'] = dict(sorted(metadata.items()))
        snapshot['acl'] = str(acl or '')
        for name, value in tuple(snapshot.items()):
            if value is not None and name not in {
                'content_length', 'metadata', 'last_modified', 'version_id',
            }:
                snapshot[name] = str(value)
        if not snapshot['etag']:
            raise CommandError(f'OSS 对象缺少 ETag，拒绝清理: {key}')
        return snapshot

    @staticmethod
    def _same_object(left, right):
        return all(left.get(name) == right.get(name) for name in OBJECT_IDENTITY_FIELDS)

    @staticmethod
    def _same_object_state(left, right):
        return all(left.get(name) == right.get(name) for name in OBJECT_STATE_FIELDS)

    @staticmethod
    def _matches_expected_state(actual, expected):
        return all(actual.get(name) == value for name, value in expected.items())

    def _validate_report_key(self, key):
        if not isinstance(key, str) or not key or key.startswith(QUARANTINE_PREFIX):
            raise CommandError(f'非法 OSS 报告 key: {key!r}')
        valid = bool(AGENT_OBJECT_RE.fullmatch(key))
        if '/' not in key:
            valid = valid or any(regex.fullmatch(key) for regex in (
                ROOT_RUN_RE, ROOT_TASK_RE, ROOT_ATTRIBUTE_RE, ROOT_UUID_RE,
            ))
        if not valid or '..' in key or key.startswith('/'):
            raise CommandError(f'OSS key 不在批准的报告范围内: {key}')
        return key

    def _validate_batch_id(self, batch_id):
        if not isinstance(batch_id, str) or not BATCH_ID_RE.fullmatch(batch_id):
            raise CommandError(f'非法清理 batch_id: {batch_id!r}')
        return batch_id

    def _quarantine_key(self, batch_id, source_key):
        self._validate_batch_id(batch_id)
        self._validate_report_key(source_key)
        return f'{QUARANTINE_PREFIX}{batch_id}/{source_key}'

    def _recover_quarantine_objects(self, object_keys, batch_id):
        """Rebuild descriptors after a worker crash between OSS copy/delete and DB persistence."""
        if not object_keys:
            return {}
        self._validate_batch_id(batch_id)
        oss, client, bucket = _client()
        recovered = {}
        for source_key in object_keys:
            self._validate_report_key(source_key)
            target_key = self._quarantine_key(batch_id, source_key)
            target = self._object_snapshot(oss, client, bucket, target_key)
            if target is None:
                continue
            source = self._object_snapshot(oss, client, bucket, source_key)
            if source is not None and not self._same_object(source, target):
                raise CommandError(f'OSS 原对象与已有隔离副本内容不一致: {source_key}')
            recovered[source_key] = {
                'quarantine_key': target_key,
                'source': source or dict(target),
                'quarantine': target,
            }
        return recovered

    def _quarantine_objects(self, object_keys, batch_id, *, expected_states=None):
        oss, client, bucket = _client()
        expected_states = expected_states or {}
        if not set(expected_states).issubset(object_keys):
            raise CommandError('OSS 对象身份约束包含规划外 key')
        quarantined = {}
        for index, source_key in enumerate(object_keys, 1):
            target_key = self._quarantine_key(batch_id, source_key)
            source = self._object_snapshot(oss, client, bucket, source_key)
            target = self._object_snapshot(oss, client, bucket, target_key)
            if source is None:
                if target is not None:
                    raise CommandError(
                        f'OSS 原对象缺失但隔离对象已存在，拒绝覆盖恢复证据: {source_key}'
                    )
                continue
            expected = expected_states.get(source_key)
            if expected is not None:
                if not isinstance(expected, dict) or set(expected) != {
                    'content_length', 'last_modified',
                }:
                    raise CommandError(f'OSS 对象身份约束非法: {source_key}')
                if not self._matches_expected_state(source, expected):
                    raise CommandError(f'OSS 对象已不同于 dry-run 快照: {source_key}')
            if target is None:
                client.copy_object(oss.CopyObjectRequest(
                    bucket=bucket,
                    key=target_key,
                    source_bucket=bucket,
                    source_key=source_key,
                    if_match=source['etag'],
                    acl=source['acl'] or None,
                    storage_class=source['storage_class'] or None,
                    metadata_directive='COPY',
                    forbid_overwrite=True,
                ))
                client.put_object_acl(oss.PutObjectAclRequest(
                    bucket=bucket, key=target_key, acl=source['acl'],
                ))
                target = self._object_snapshot(oss, client, bucket, target_key)
            current_source = self._object_snapshot(oss, client, bucket, source_key)
            if current_source is None or not self._same_object_state(current_source, source):
                raise CommandError(f'OSS 隔离期间原对象发生变化: {source_key}')
            if target is None or not self._same_object(target, source):
                raise CommandError(f'OSS 隔离完整性校验失败: {source_key}')
            quarantined[source_key] = {
                'quarantine_key': target_key,
                'source': source,
                'quarantine': target,
            }
            if index % 500 == 0:
                self.stdout.write(f'quarantined={index}/{len(object_keys)}')
        return quarantined

    def _validate_quarantine_descriptor(self, original, descriptor, batch_id):
        self._validate_report_key(original)
        if not isinstance(descriptor, dict):
            raise CommandError(f'非法隔离描述: {original}')
        expected_key = self._quarantine_key(batch_id, original)
        if descriptor.get('quarantine_key') != expected_key:
            raise CommandError(f'隔离 key 与 batch 不匹配: {original}')
        for state_name in ('source', 'quarantine'):
            state = descriptor.get(state_name)
            if not isinstance(state, dict) or set(state) != set(OBJECT_STATE_FIELDS):
                raise CommandError(f'隔离对象状态字段不完整: {original}/{state_name}')
        if not self._same_object(descriptor['source'], descriptor['quarantine']):
            raise CommandError(f'隔离对象与原对象身份不一致: {original}')

    def _delete_objects(self, quarantine_map):
        if not quarantine_map:
            return
        oss, client, bucket = _client()
        batch_id = self._batch_id_from_quarantine_map(quarantine_map)
        present = []
        for original, descriptor in sorted(quarantine_map.items()):
            self._validate_quarantine_descriptor(original, descriptor, batch_id)
            source = self._object_snapshot(oss, client, bucket, original)
            target = self._object_snapshot(oss, client, bucket, descriptor['quarantine_key'])
            if target is None or not self._same_object_state(target, descriptor['quarantine']):
                raise CommandError(f'删除前隔离对象校验失败: {original}')
            if source is None:
                continue
            if not self._same_object_state(source, descriptor['source']):
                raise CommandError(f'删除前原对象发生变化: {original}')
            present.append(original)

        for start in range(0, len(present), 1000):
            keys = present[start:start + 1000]
            result = client.delete_multiple_objects(oss.DeleteMultipleObjectsRequest(
                bucket=bucket,
                objects=[oss.DeleteObject(key=key) for key in keys],
                quiet=False,
            ))
            deleted = {
                str(item.key) for item in (getattr(result, 'deleted_objects', None) or ())
            }
            if deleted != set(keys):
                missing = sorted(set(keys) - deleted)
                raise CommandError(f'OSS 批量删除未确认全部对象: {missing[:5]}')
            remaining = [
                key for key in keys
                if self._head_object(oss, client, bucket, key) is not None
            ]
            if remaining:
                raise CommandError(f'OSS 原对象删除后仍存在: {remaining[:5]}')

    def _restore_objects(self, quarantine_map):
        if not quarantine_map:
            return
        oss, client, bucket = _client()
        batch_id = self._batch_id_from_quarantine_map(quarantine_map)
        for index, (original, descriptor) in enumerate(sorted(quarantine_map.items()), 1):
            self._validate_quarantine_descriptor(original, descriptor, batch_id)
            current = self._object_snapshot(oss, client, bucket, original)
            # Recovery is idempotent: a prior attempt may already have restored the
            # original and then partially purged quarantine copies before failing.
            if current is not None:
                if not self._same_object(current, descriptor['source']):
                    raise CommandError(f'OSS 原 key 已被不同内容占用: {original}')
                continue
            target = self._object_snapshot(oss, client, bucket, descriptor['quarantine_key'])
            if target is None or not self._same_object_state(target, descriptor['quarantine']):
                raise CommandError(f'隔离对象不存在或内容变化，无法恢复: {descriptor["quarantine_key"]}')
            client.copy_object(oss.CopyObjectRequest(
                bucket=bucket,
                key=original,
                source_bucket=bucket,
                source_key=descriptor['quarantine_key'],
                if_match=descriptor['quarantine']['etag'],
                acl=descriptor['source']['acl'] or None,
                storage_class=descriptor['source']['storage_class'] or None,
                metadata_directive='COPY',
                forbid_overwrite=True,
            ))
            client.put_object_acl(oss.PutObjectAclRequest(
                bucket=bucket, key=original, acl=descriptor['source']['acl'],
            ))
            restored = self._object_snapshot(oss, client, bucket, original)
            if restored is None or not self._same_object(restored, descriptor['source']):
                raise CommandError(f'OSS 恢复完整性校验失败: {original}')
            if index % 500 == 0:
                self.stdout.write(f'restored_oss={index}/{len(quarantine_map)}')

    def _purge_quarantine_objects(self, quarantine_map):
        """Delete only verified copies under this operation's quarantine prefix."""
        if not quarantine_map:
            return
        oss, client, bucket = _client()
        batch_id = self._batch_id_from_quarantine_map(quarantine_map)
        keys = []
        for original, descriptor in sorted(quarantine_map.items()):
            self._validate_quarantine_descriptor(original, descriptor, batch_id)
            quarantine_key = descriptor['quarantine_key']
            target = self._object_snapshot(oss, client, bucket, quarantine_key)
            if target is None:
                continue
            if not self._same_object_state(target, descriptor['quarantine']):
                raise CommandError(f'隔离副本清理前发生变化: {quarantine_key}')
            keys.append(quarantine_key)

        for start in range(0, len(keys), 1000):
            batch = keys[start:start + 1000]
            result = client.delete_multiple_objects(oss.DeleteMultipleObjectsRequest(
                bucket=bucket,
                objects=[oss.DeleteObject(key=key) for key in batch],
                quiet=False,
            ))
            deleted = {
                str(item.key) for item in (getattr(result, 'deleted_objects', None) or ())
            }
            if deleted != set(batch):
                missing = sorted(set(batch) - deleted)
                raise CommandError(f'OSS 隔离副本批量删除未确认全部对象: {missing[:5]}')
            remaining = [
                key for key in batch
                if self._head_object(oss, client, bucket, key) is not None
            ]
            if remaining:
                raise CommandError(f'OSS 隔离副本删除后仍存在: {remaining[:5]}')

    def _batch_id_from_quarantine_map(self, quarantine_map):
        batch_ids = set()
        for original, descriptor in quarantine_map.items():
            key = str((descriptor or {}).get('quarantine_key') or '')
            suffix = '/' + original
            if not key.startswith(QUARANTINE_PREFIX) or not key.endswith(suffix):
                raise CommandError(f'非法隔离映射: {original}')
            batch_ids.add(key[len(QUARANTINE_PREFIX):-len(suffix)])
        if len(batch_ids) != 1:
            raise CommandError('隔离映射包含多个或缺失 batch_id')
        return self._validate_batch_id(batch_ids.pop())

    def _assert_simc_queue_drained(self):
        active_task_ids = list(
            SimcTask.objects.filter(current_status__in=(0, 1))
            .order_by('id').values_list('id', flat=True)[:6]
        )
        active_execution_ids = list(
            SimcBenchmarkExecution.objects.filter(status__in=(
                SimcBenchmarkExecution.STATUS_PENDING,
                SimcBenchmarkExecution.STATUS_RUNNING,
            )).order_by('id').values_list('id', flat=True)[:6]
        )
        if active_task_ids or active_execution_ids:
            raise CommandError(
                'SimC 队列未排空，拒绝进入维护清理；'
                f'active_task_ids={active_task_ids[:5]}, '
                f'active_execution_ids={active_execution_ids[:5]}'
            )

    def _lock_cleanup_rows(self):
        # Lock every possible report owner and all Benchmark graph rows. The
        # one-time maintenance may briefly delay workers, but it prevents a new
        # Artifact reference from racing the final OSS orphan check.
        model_querysets = (
            SimcBenchmarkPanel.objects.all(),
            SimcBenchmarkSpec.objects.all(),
            SimcBenchmarkProfile.objects.all(),
            SimcBenchmarkScenario.objects.all(),
            SimcBenchmarkCandidate.objects.all(),
            SimcBenchmarkExecution.objects.all(),
            SimcTask.objects.all(),
            SimcBenchmarkCase.objects.all(),
            SimulationRun.objects.all(),
            SimcTaskArtifact.objects.all(),
            SimcBenchmarkResult.objects.all(),
            SimcTaskFavorite.objects.all(),
        )
        for queryset in model_querysets:
            list(queryset.select_for_update().order_by('pk').values_list('pk', flat=True))

    def _validate_locked_plan(self, plan):
        if plan.warnings:
            raise CommandError('事务锁定后出现规划告警，拒绝删除')
        self._assert_simc_queue_drained()
        if SimcTask.objects.filter(
            id__in=plan.deletable_task_ids, current_status__in=(0, 1),
        ).exists():
            raise CommandError('待删除集合出现待执行/执行中 Task，拒绝删除')
        if SimcTaskFavorite.objects.filter(task_id__in=plan.deletable_task_ids).exists():
            raise CommandError('待删除集合出现收藏 Task，拒绝删除')
        if SimcTask.objects.exclude(id__in=plan.deletable_task_ids).filter(
            source_task_id__in=plan.deletable_task_ids,
        ).exists():
            raise CommandError('待删除 Task 仍被保留 Task 的 source_task 引用')
        if SimcBenchmarkCase.objects.exclude(id__in=plan.deletable_case_ids).filter(
            task_id__in=plan.deletable_task_ids,
        ).exists():
            raise CommandError('待删除 Task 仍被保留 Benchmark Case 引用')
        if set(SimulationRun.objects.filter(
            task_id__in=plan.deletable_task_ids,
        ).values_list('id', flat=True)) != set(plan.run_ids):
            raise CommandError('待删除 Task 的 Run 集合与 manifest 不一致')
        if set(SimcTaskArtifact.objects.filter(
            task_id__in=plan.deletable_task_ids,
        ).values_list('id', flat=True)) != set(plan.artifact_ids):
            raise CommandError('待删除 Task 的 Artifact 集合与 manifest 不一致')
        if set(SimcBenchmarkResult.objects.filter(
            case_id__in=plan.deletable_case_ids,
        ).values_list('id', flat=True)) != set(plan.result_ids):
            raise CommandError('待删除 Case 的 Result 集合与 manifest 不一致')
        if SimcBenchmarkCase.objects.exclude(id__in=plan.deletable_case_ids).filter(
            execution_id__in=plan.deletable_execution_ids,
        ).exists():
            raise CommandError('待删除 Execution 仍包含保留 Case')

    def _validate_orphans_unreferenced(self, orphans):
        orphan_keys = {row.key for row in orphans}
        if not orphan_keys:
            return
        referenced = {
            key for path in SimcTaskArtifact.objects.values_list('file_path', flat=True)
            if (key := _artifact_object_key(path))
        }
        raced = sorted(orphan_keys & referenced)
        if raced:
            raise CommandError(f'纯 OSS 孤儿已获得 Artifact 引用，拒绝删除: {raced[:5]}')

    @staticmethod
    def _delete_artifact_rows(plan):
        """Drop only report indexes; every execution-history row is immutable here."""
        SimcTaskArtifact.objects.filter(id__in=plan.artifact_ids).delete()
        if SimcTaskArtifact.objects.filter(id__in=plan.artifact_ids).exists():
            raise CommandError('Artifact 索引删除后仍有残留记录')

        retained_checks = {
            'results': (SimcBenchmarkResult, plan.result_ids),
            'cases': (SimcBenchmarkCase, plan.deletable_case_ids),
            'runs': (SimulationRun, plan.run_ids),
            'tasks': (SimcTask, plan.deletable_task_ids),
            'executions': (SimcBenchmarkExecution, plan.deletable_execution_ids),
        }
        missing = sorted(
            name for name, (model, ids) in retained_checks.items()
            if set(model.objects.filter(id__in=ids).values_list('id', flat=True)) != set(ids)
        )
        if missing:
            raise CommandError(f'运行历史记录意外变化，事务回滚: {missing}')

    def _validate_backup(self, backup):
        if backup.get('backup_schema') != BACKUP_SCHEMA:
            raise CommandError('不支持的备份版本')
        plan = backup.get('plan')
        if not isinstance(plan, dict) or not str(plan.get('fingerprint') or ''):
            raise CommandError('备份缺少有效清理计划')
        self._validate_batch_id(plan.get('batch_id'))
        records = backup.get('records')
        if not isinstance(records, dict) or set(records) != set(RECORD_MODELS):
            raise CommandError('备份记录分组不在白名单内')
        counts = backup.get('record_counts')
        if not isinstance(counts, dict) or set(counts) != set(RECORD_MODELS):
            raise CommandError('备份记录计数缺失')
        for group, model in RECORD_MODELS.items():
            rows = records[group]
            if not isinstance(rows, list) or counts[group] != len(rows):
                raise CommandError(f'备份记录计数不一致: {group}')
            allowed_fields = {
                field.name for field in model._meta.local_fields
                if field.serialize and not field.primary_key
            }
            expected_model = model._meta.label_lower
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != {'model', 'pk', 'fields'}:
                    raise CommandError(f'非法备份记录结构: {group}')
                if row['model'] != expected_model or not isinstance(row['fields'], dict):
                    raise CommandError(f'备份模型不在白名单内: {group}')
                if set(row['fields']) != allowed_fields:
                    raise CommandError(f'备份字段不在白名单内: {group}/{row.get("pk")}')
                if not isinstance(row['pk'], int) or row['pk'] <= 0 or row['pk'] in seen:
                    raise CommandError(f'非法或重复主键: {group}/{row.get("pk")}')
                seen.add(row['pk'])
        object_keys = backup.get('object_keys')
        if not isinstance(object_keys, list) or len(object_keys) != len(set(object_keys)):
            raise CommandError('备份 OSS key 清单非法')
        for key in object_keys:
            self._validate_report_key(key)
        quarantine_map = backup.get('quarantine_map')
        if not isinstance(quarantine_map, dict):
            raise CommandError('备份隔离映射非法')
        if quarantine_map:
            batch_id = self._batch_id_from_quarantine_map(quarantine_map)
            if batch_id != plan['batch_id']:
                raise CommandError('备份隔离映射 batch_id 不一致')
            if not set(quarantine_map).issubset(set(object_keys)):
                raise CommandError('隔离映射包含计划外 OSS key')
            for original, descriptor in quarantine_map.items():
                self._validate_quarantine_descriptor(original, descriptor, batch_id)

    def _serialized_object(self, obj):
        return json.loads(serializers.serialize('json', [obj]))[0]

    def _assert_existing_matches(self, model, row):
        existing = model._default_manager.filter(pk=row['pk']).first()
        if existing is None:
            return False
        if self._serialized_object(existing) != row:
            raise CommandError(f'回滚主键冲突且内容不同: {model._meta.label}/{row["pk"]}')
        return True

    def _restore_database_records(self, records):
        # Validate all existing rows before the first write so a conflict cannot
        # turn rollback into a partial merge.
        for group, model in RECORD_MODELS.items():
            for row in records[group]:
                self._assert_existing_matches(model, row)

        for group in RECORD_ORDER:
            model = RECORD_MODELS[group]
            for row in records[group]:
                if model._default_manager.filter(pk=row['pk']).exists():
                    continue
                encoded = json.dumps([row], ensure_ascii=False, separators=(',', ':'))
                try:
                    item = next(serializers.deserialize('json', encoded))
                    item.save()
                except Exception as exc:
                    raise CommandError(f'数据库记录恢复失败: {group}/{row["pk"]}') from exc

        for group, model in RECORD_MODELS.items():
            for row in records[group]:
                self._assert_existing_matches(model, row)

    def _rollback(self, backup_path):
        if not backup_path.exists():
            raise CommandError(f'备份不存在: {backup_path}')
        backup = self._read_backup(backup_path)
        quarantine_map = backup['quarantine_map']

        # OSS conflicts are checked before DB writes. Existing original keys are
        # accepted only when every recorded identity field is identical.
        if quarantine_map:
            oss, client, bucket = _client()
            batch_id = self._batch_id_from_quarantine_map(quarantine_map)
            for original, descriptor in quarantine_map.items():
                self._validate_quarantine_descriptor(original, descriptor, batch_id)
                target = self._object_snapshot(oss, client, bucket, descriptor['quarantine_key'])
                if target is None or not self._same_object(target, descriptor['quarantine']):
                    raise CommandError(f'隔离对象不存在或内容变化，无法回滚: {original}')
                current = self._object_snapshot(oss, client, bucket, original)
                if current is not None and not self._same_object(current, descriptor['source']):
                    raise CommandError(f'OSS 原 key 已被不同内容占用: {original}')

        with transaction.atomic():
            self._lock_cleanup_rows()
            self._restore_database_records(backup['records'])
            self._restore_objects(quarantine_map)

        self.stdout.write(self.style.SUCCESS(
            f'rollback complete backup={backup_path.resolve()} '
            f'fingerprint={backup["plan"]["fingerprint"]}'
        ))
