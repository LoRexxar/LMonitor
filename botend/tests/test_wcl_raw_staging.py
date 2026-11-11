import multiprocessing
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from botend.services.wcl_raw_staging import stage_events, load_staged_events, prune_staging


def persisted_event(**filters):
    from botend.models import WclCombatantFact, WclCombatantSnapshot
    from botend.services.wcl_combatant_store import load_facts
    row = WclCombatantFact.objects.get(**filters)
    if WclCombatantSnapshot.objects.filter(report_code=row.report_code,
            fight_id=row.fight_id, actor_id=row.actor_id).exists():
        raise AssertionError('New collection must not write legacy raw snapshots')
    events = load_facts([{'report_code': row.report_code, 'fight_id': row.fight_id}])
    return next(event for event in events[row.report_code, row.fight_id]
                if event['sourceID'] == row.actor_id)


def hold_lock(path, ready, release):
    import fcntl
    with open(path, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready.set()
        release.wait(5)


class RawStagingIsolationMixin:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = override_settings(WCL_RAW_STAGING_DIR=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)


class RawStagingTests(RawStagingIsolationMixin, SimpleTestCase):
    def events(self):
        return {1: [{'fight': 1, 'sourceID': 3, 'unknown': {'keep': [1, 2]}}], 2: []}

    def test_complete_roundtrip_integrity_and_missing_fights(self):
        result = stage_events('private-report', self.events())
        self.assertEqual(result['status'], 'stored')
        self.assertEqual(load_staged_events('private-report', [1, 2]), self.events()[1])
        self.assertIsNone(load_staged_events('private-report', [1, 3]))
        from django.conf import settings
        files = list(Path(settings.WCL_RAW_STAGING_DIR).glob('*.gz'))
        self.assertEqual(len(files), 2)
        self.assertNotIn('private-report', ''.join(p.name for p in files))
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        files[0].write_bytes(b'broken')
        self.assertIsNone(load_staged_events('private-report', [1, 2]))

    @override_settings(WCL_RAW_STAGING_TTL_SECONDS=1)
    def test_expiry_maintained_on_read(self):
        with patch('botend.services.wcl_raw_staging.time.time', return_value=100):
            stage_events('code', self.events())
        with patch('botend.services.wcl_raw_staging.time.time', return_value=102):
            self.assertIsNone(load_staged_events('code', [1]))
        self.assertEqual(prune_staging()['bytes'], 0)

    @override_settings(WCL_RAW_STAGING_MAX_BYTES=100, WCL_RAW_STAGING_MAX_FILE_BYTES=100)
    def test_quota_is_not_exceeded(self):
        result = stage_events('code', self.events())
        self.assertEqual(result['status'], 'quota')
        self.assertLessEqual(prune_staging()['bytes'], 100)

    def test_symlink_refused(self):
        from django.conf import settings
        target = Path(settings.WCL_RAW_STAGING_DIR) / '.lock'
        with tempfile.NamedTemporaryFile() as outside:
            target.symlink_to(outside.name)
            self.assertEqual(stage_events('code', self.events())['status'], 'unsafe_path')
            self.assertIsNone(load_staged_events('code', [1]))
            self.assertEqual(os.stat(outside.name).st_size, 0)

    def test_unknown_directory_contents_are_preserved_without_chmod(self):
        from django.conf import settings
        root = Path(settings.WCL_RAW_STAGING_DIR)
        important = root / 'unrelated.json'
        important.write_text('keep')
        root.chmod(0o750)
        self.assertEqual(stage_events('code', self.events())['status'], 'unsafe_path')
        self.assertEqual(important.read_text(), 'keep')
        self.assertEqual(root.stat().st_mode & 0o777, 0o750)

    def test_empty_media_root_still_rejects_public_fallback(self):
        from django.conf import settings
        with override_settings(BASE_DIR=settings.WCL_RAW_STAGING_DIR, MEDIA_ROOT='',
                WCL_RAW_STAGING_DIR=str(Path(settings.WCL_RAW_STAGING_DIR) / 'media' / 'raw')):
            self.assertEqual(stage_events('code', self.events())['status'], 'unsafe_path')

    def test_dotdot_is_rejected_before_creating_any_path_component(self):
        from django.conf import settings
        root = Path(settings.WCL_RAW_STAGING_DIR)
        media = root / 'media'
        media.mkdir()
        mode = media.stat().st_mode
        traversal = root / 'not-created' / '..' / 'media' / 'raw'
        with override_settings(MEDIA_ROOT=str(media), WCL_RAW_STAGING_DIR=str(traversal)):
            self.assertEqual(stage_events('code', self.events())['status'], 'unsafe_path')
            self.assertIsNone(load_staged_events('code', [1]))
            self.assertEqual(prune_staging()['status'], 'unsafe_path')
        self.assertFalse((root / 'not-created').exists())
        self.assertEqual(list(media.iterdir()), [])
        self.assertEqual(media.stat().st_mode, mode)

    def test_bad_limits_and_timestamp_are_best_effort(self):
        for key in ('WCL_RAW_STAGING_TTL_SECONDS', 'WCL_RAW_STAGING_MAX_BYTES',
                    'WCL_RAW_STAGING_MAX_FILE_BYTES'):
            with self.subTest(key=key), override_settings(**{key: 'invalid'}):
                self.assertEqual(stage_events('code', self.events())['status'], 'io_error')
                self.assertIsNone(load_staged_events('code', [1]))
                self.assertEqual(prune_staging()['status'], 'io_error')
        self.assertEqual(stage_events('code', self.events(), fetched_at=object())['status'], 'io_error')

    def test_one_cache_read_does_not_decode_all_other_cached_responses(self):
        from botend.services import wcl_raw_staging as staging
        for i in range(5):
            stage_events('report-' + str(i), self.events())
        with patch.object(staging, '_read', wraps=staging._read) as read:
            self.assertIsNotNone(load_staged_events('report-0', [1]))
        self.assertEqual(read.call_count, 1)

    @override_settings(WCL_RAW_STAGING_TTL_SECONDS=10)
    def test_replay_does_not_extend_ttl(self):
        with patch('botend.services.wcl_raw_staging.time.time', return_value=100):
            stage_events('code', self.events())
        with patch('botend.services.wcl_raw_staging.time.time', return_value=109):
            stage_events('code', self.events())
        with patch('botend.services.wcl_raw_staging.time.time', return_value=111):
            self.assertIsNone(load_staged_events('code', [1, 2]))

    def test_cross_process_lock_serializes_writes(self):
        from django.conf import settings
        ready, release = multiprocessing.Event(), multiprocessing.Event()
        process = multiprocessing.Process(target=hold_lock,
            args=(str(Path(settings.WCL_RAW_STAGING_DIR) / '.lock'), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(3))
            import threading
            result = []
            writer = threading.Thread(target=lambda: result.append(stage_events('code', self.events())))
            writer.start()
            writer.join(.1)
            self.assertTrue(writer.is_alive())
            release.set()
            writer.join(3)
            self.assertEqual(result[0]['status'], 'stored')
        finally:
            release.set()
            process.join(3)


class RawStagingRecoveryTests(RawStagingIsolationMixin, TransactionTestCase):
    def collect_with_bad_cache(self, corruption):
        import gzip
        import json
        import zlib
        from unittest.mock import Mock
        from django.conf import settings
        from botend.models import WclCombatantFact, WclCombatantSnapshot, WclCombatantFetchState
        from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
        from botend.tests.test_wcl_combatant_snapshot import event, sample
        payload = event()
        self.assertEqual(stage_events('Report', {11: [payload]})['status'], 'stored')
        path, = Path(settings.WCL_RAW_STAGING_DIR).glob('*.gz')
        packed = path.read_bytes()
        if corruption == 'truncated':
            packed = packed[:-4]
        elif corruption == 'deflate':
            # Reserved DEFLATE BTYPE=3, with an otherwise real gzip envelope.
            packed = packed[:10] + b'\x07' + packed[-8:]
            with self.assertRaises(zlib.error):
                gzip.decompress(packed)
        else:
            envelope = json.loads(gzip.decompress(packed))
            del envelope['events']
            packed = gzip.compress(json.dumps(envelope).encode())
        path.write_bytes(packed)
        fetcher = Mock(_wcl_last_error='')
        fetcher.fetch_wcl_combatant_info.return_value = [payload]
        result = collect_dungeon_combatants([sample()], fetcher, apply=True)
        fetcher.fetch_wcl_combatant_info.assert_called_once_with('Report', [11])
        self.assertEqual((result['matched'], result['failed'], result['snapshots_written']), (1, 0, 1))
        self.assertEqual(WclCombatantFact.objects.count(), 1)
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertEqual(persisted_event()['sourceID'], payload['sourceID'])

    def test_truncated_gzip_does_not_block_successful_http_fact(self):
        self.collect_with_bad_cache('truncated')

    def test_bad_deflate_does_not_block_successful_http_fact(self):
        self.collect_with_bad_cache('deflate')

    def test_missing_envelope_field_does_not_block_successful_http_fact(self):
        self.collect_with_bad_cache('envelope')

    @override_settings(WCL_RAW_STAGING_TTL_SECONDS='invalid')
    def test_bad_cache_configuration_does_not_block_successful_http_fact(self):
        from unittest.mock import Mock
        from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
        from botend.models import WclCombatantFact
        from botend.tests.test_wcl_combatant_snapshot import event, sample
        fetcher = Mock(_wcl_last_error='')
        fetcher.fetch_wcl_combatant_info.return_value = [event()]
        result = collect_dungeon_combatants([sample()], fetcher, apply=True)
        fetcher.fetch_wcl_combatant_info.assert_called_once_with('Report', [11])
        self.assertEqual((result['matched'], result['failed'], result['snapshots_written']), (1, 0, 1))
        self.assertEqual(WclCombatantFact.objects.count(), 1)

    @override_settings(WCL_RAW_STAGING_DIR=None)
    def test_none_cache_directory_does_not_fail_committed_http_fact(self):
        from unittest.mock import Mock
        from django.db import connection
        from botend.models import WclCombatantFact, WclCombatantSnapshot
        from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
        from botend.tests.test_wcl_combatant_snapshot import event, sample
        fetcher = Mock(_wcl_last_error='')
        fetcher.fetch_wcl_combatant_info.return_value = [event()]
        # TransactionTestCase leaves the real on_commit callback executable.
        self.assertFalse(connection.in_atomic_block)
        with self.assertLogs('botend.services.wcl_raw_staging', level='WARNING') as logs:
            result = collect_dungeon_combatants([sample()], fetcher, apply=True)
        fetcher.fetch_wcl_combatant_info.assert_called_once_with('Report', [11])
        self.assertEqual(WclCombatantFact.objects.count(), 1)
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        self.assertEqual((result['matched'], result['failed'], result['snapshots_written']), (1, 0, 1))
        # Load, stage and post-commit discard all reached the cache boundary.
        self.assertEqual(len(logs.records), 3)
        self.assertEqual(connection.run_on_commit, [])

    def test_db_failure_replays_raw_without_another_source_request(self):
        from unittest.mock import Mock
        from datetime import timedelta
        from django.utils import timezone
        from django.db import DatabaseError
        from botend.models import WclCombatantFact, WclCombatantSnapshot, WclCombatantFetchState
        from botend.services import wcl_combatant_snapshot as service
        from botend.tests.test_wcl_combatant_snapshot import event, sample
        payload = {**event(), 'unknown_transport': {'original': [1, 2]}}
        fetcher = Mock(_wcl_last_error='')
        fetcher.fetch_wcl_combatant_info.return_value = [payload]
        original_time = timezone.now() - timedelta(hours=12)
        with patch.object(service.wcl_combatant_store, 'persist_events', side_effect=DatabaseError), \
                patch('django.utils.timezone.now', return_value=original_time):
            failed = service.collect_dungeon_combatants([sample()], fetcher, apply=True)
        self.assertEqual((failed['failed'], failed['matched']), (1, 0))
        self.assertFalse(WclCombatantFact.objects.exists())
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertEqual(load_staged_events('Report', [11]), [payload])
        self.assertEqual(load_staged_events('Report', [11], with_fetched_at=True),
                         ([payload], {11: original_time}))
        recovered = service.collect_dungeon_combatants([sample()], fetcher, apply=True)
        self.assertEqual((recovered['matched'], recovered['snapshots_written']), (1, 1))
        fetcher.fetch_wcl_combatant_info.assert_called_once()
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        self.assertEqual(WclCombatantFact.objects.get().fetched_at, original_time)
        self.assertIsNone(load_staged_events('Report', [11]))

    @override_settings(WCL_RAW_STAGING_MAX_BYTES=1)
    def test_cache_quota_failure_does_not_discard_successful_facts(self):
        from unittest.mock import Mock
        from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
        from botend.tests.test_wcl_combatant_snapshot import event, sample
        fetcher = Mock(_wcl_last_error='')
        fetcher.fetch_wcl_combatant_info.return_value = [event()]
        result = collect_dungeon_combatants([sample()], fetcher, apply=True)
        self.assertEqual((result['matched'], result['failed']), (1, 0))
        self.assertIsNotNone(persisted_event())
        fetcher.fetch_wcl_combatant_info.assert_called_once()
