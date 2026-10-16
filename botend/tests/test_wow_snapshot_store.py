"""原始资料合并的零重复写入与字段隔离回归。"""
from datetime import timedelta
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from botend.models import WowSpellSnapshot, WowSpellEffectSnapshot
from botend.services.wow_snapshot_store import upsert_changed_snapshots


class ChangedSnapshotTests(TestCase):
    def merge(self, rows, fields=None):
        return upsert_changed_snapshots(WowSpellSnapshot, rows, ['branch', 'locale', 'spell_id'],
                                       fields or ['name', 'description', 'snapshot_build', 'updated_at'])

    def test_identical_raw_values_do_not_write_or_advance_time(self):
        old = WowSpellSnapshot.objects.create(spell_id=101, name='中文技能', description='原文', snapshot_build='来源')
        incoming = WowSpellSnapshot(spell_id=101, name='中文技能', description='原文', snapshot_build='来源',
                                    updated_at=old.updated_at + timedelta(hours=1))
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.merge([incoming]), 0)
        self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in ('UPDATE', 'INSERT', 'DELETE') for q in queries))
        self.assertEqual(WowSpellSnapshot.objects.get(pk=old.pk).updated_at, old.updated_at)

    def test_changed_source_updates_same_row_without_touching_final_projection(self):
        old = WowSpellSnapshot.objects.create(spell_id=102, name='旧名', name_zh='人工中文', display_data={'保留': True})
        incoming = WowSpellSnapshot(spell_id=102, name='新名', updated_at=timezone.now())
        self.assertEqual(self.merge([incoming], ['name', 'updated_at']), 1)
        old.refresh_from_db()
        self.assertEqual((old.name, old.name_zh, old.display_data), ('新名', '人工中文', {'保留': True}))
        self.assertEqual(WowSpellSnapshot.objects.count(), 1)

    def test_batch_deduplicates_identity_and_separates_branches(self):
        self.assertEqual(self.merge([WowSpellSnapshot(spell_id=103, name='第一条'),
            WowSpellSnapshot(spell_id=103, name='最后一条'),
            WowSpellSnapshot(branch='wowxptr', spell_id=103, name='测试服')]), 2)
        self.assertEqual(WowSpellSnapshot.objects.get(branch='wow', spell_id=103).name, '最后一条')
        self.assertEqual(WowSpellSnapshot.objects.get(branch='wowxptr', spell_id=103).name, '测试服')

    def test_new_source_build_is_a_real_change(self):
        old = WowSpellSnapshot.objects.create(spell_id=104, snapshot_build='旧来源')
        self.assertEqual(self.merge([WowSpellSnapshot(spell_id=104, snapshot_build='新来源')]), 1)
        old.refresh_from_db()
        self.assertEqual(old.snapshot_build, '新来源')

    def test_effect_zero_and_null_are_different_but_duplicate_poll_is_noop(self):
        row = WowSpellEffectSnapshot.objects.create(spell_id=105, effect_index=0, misc_value=None)
        fields = ['misc_value', 'base_points', 'updated_at']
        keys = ['branch', 'locale', 'spell_id', 'effect_index']
        def merge():
            return upsert_changed_snapshots(WowSpellEffectSnapshot,
                [WowSpellEffectSnapshot(spell_id=105, effect_index=0, misc_value=0)], keys, fields)
        self.assertEqual(merge(), 1)
        row.refresh_from_db()
        before = row.updated_at
        self.assertEqual(merge(), 0)
        row.refresh_from_db()
        self.assertEqual((row.misc_value, row.updated_at), (0, before))
