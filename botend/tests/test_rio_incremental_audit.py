"""同日正文校验证明及备份复用不掩盖损坏。"""
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from botend.services import rio_statistics as rio
from botend.services.rio_statistics_storage import SnapshotStore, StorageError
from botend.tests import test_rio_statistics as fixtures


class IncrementalAuditTests(SimpleTestCase):
    setUp = fixtures.RioStatisticsTests.setUp

    def publish(self, region='cn', value=1):
        c = rio.coordinate('overview', {'region': region})
        rio._publish(c, {'value': value})
        return c

    def test_new_instance_reuses_only_same_day_unchanged_proofs(self):
        self.publish()
        store = rio.store()
        self.assertEqual(store.audit(deep=True, reuse_verified=True)['verified_payloads'], 2)
        other = SnapshotStore(store.root)
        with patch.object(other, '_unpack', wraps=other._unpack) as unpack:
            report = other.audit(deep=True, reuse_verified=True)
            self.assertEqual(report['reused_payloads'], 2)
            unpack.assert_not_called()
            self.publish('eu')
            report = other.audit(deep=True, reuse_verified=True)
            self.assertEqual((report['verified_payloads'], report['reused_payloads']), (2, 2))
            self.assertEqual(unpack.call_count, 2)
        with patch('django.utils.timezone.now', return_value=timezone.now()+timedelta(days=1)):
            report = other.audit(deep=True, reuse_verified=True)
            self.assertEqual((report['verified_payloads'], report['reused_payloads']), (4, 0))

    def test_changed_body_and_metadata_cannot_reuse_validation(self):
        c = self.publish()
        self.publish('eu')
        store = rio.store()
        store.audit(deep=True, reuse_verified=True)
        with store.connection() as db:
            db.execute('UPDATE snapshots SET payload=? WHERE key=?', (b'corrupt', rio.key_for(c)))
            db.execute("UPDATE history SET scope='changed'")
        report = store.audit(deep=True, reuse_verified=True)
        self.assertEqual(report['state'], 'error')
        self.assertEqual(report['invalid_payloads'], [rio.key_for(c)])
        self.assertEqual(len(report['invalid_history']), 2)
        with self.assertRaises(StorageError):
            store.backup()

    def test_empty_blob_is_corruption_not_a_verified_missing_payload(self):
        c = self.publish()
        store = rio.store()
        store.audit(deep=True, reuse_verified=True)
        with store.connection() as db:
            db.execute('UPDATE snapshots SET payload=? WHERE key=?', (b'', rio.key_for(c)))
        self.assertEqual(store.audit(deep=True, reuse_verified=True)['invalid_payloads'], [rio.key_for(c)])

    def test_backup_reuses_verified_file_but_replaces_modified_or_changed_data(self):
        self.publish()
        store = rio.store()
        first = store.backup()
        self.assertFalse(first['reused'])
        self.assertTrue(store.backup()['reused'])
        destination = store.root / 'backups' / first['path']
        with destination.open('ab') as stream:
            stream.write(b'changed')
        self.assertFalse(store.backup()['reused'])
        self.publish(value=2)
        self.assertFalse(store.backup()['reused'])
        # 人工完整深检仍实际解压全部正文，不使用维护证明。
        with patch.object(store, '_unpack', wraps=store._unpack) as unpack:
            self.assertEqual(store.audit(deep=True)['state'], 'ok')
        self.assertEqual(unpack.call_count, 2)
