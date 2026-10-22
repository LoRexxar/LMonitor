"""Publication coverage regressions: no synthetic ready marker is evidence."""
from unittest.mock import patch
from django.test import TestCase
from botend.tests import test_journal_loot_snapshot as base
from botend.services import journal_loot_snapshot as snapshots


class JournalSnapshotCoverageTests(TestCase):
    setUp = base.JournalLootSnapshotTests.setUp
    read = base.JournalLootSnapshotTests.read
    def test_unresolved_equipment_cannot_publish_or_replace_previous_file(self):
        self.read()
        key = snapshots._load(next(snapshots.snapshot_root().glob('*/request.json')))
        directory = snapshots._directory(key)
        old = b'{"schema1_last_good":"must remain untouched"}'
        (directory / 'data.json').write_bytes(old)
        before = (directory / 'data.json').stat().st_mtime_ns
        from botend.portal.adventure_journal import build_loot_projection
        def unresolved(*args):
            result = build_loot_projection(*args)
            result['loot'][0]['details'].update(status='unresolved', complete=False)
            return result
        with patch('botend.portal.adventure_journal.build_loot_projection', side_effect=unresolved):
            with self.assertRaises(RuntimeError):
                snapshots.refresh_journal_loot_snapshots(force=True, strict=True, targets=[key])
        self.assertEqual((directory / 'data.json').read_bytes(), old)
        self.assertEqual((directory / 'data.json').stat().st_mtime_ns, before)
        self.assertIsNone(snapshots._read_projection(key))

    def test_empty_payload_cannot_publish_nonempty_boss_scope(self):
        self.read()
        key = snapshots._load(next(snapshots.snapshot_root().glob('*/request.json')))
        with patch('botend.portal.adventure_journal.build_loot_projection', return_value={'loot': [], 'loot_total': 0}):
            with self.assertRaises(RuntimeError):
                snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        self.assertFalse((snapshots._directory(key) / 'data.json').exists())

    def target(self):
        self.read()
        return snapshots._load(next(snapshots.snapshot_root().glob('*/request.json')))

    def test_missing_real_difficulty_variant_fails_and_keeps_last_good(self):
        from botend.models import WowItemVariantSnapshot
        key = self.target()
        snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        path = snapshots._directory(key) / 'data.json'
        previous, stamp = path.read_bytes(), path.stat().st_mtime_ns
        WowItemVariantSnapshot.objects.filter(item__item_id=60).delete()
        with self.assertLogs(snapshots.logger, level='ERROR'), self.assertRaises(RuntimeError):
            snapshots.refresh_journal_loot_snapshots(strict=True, force=True, targets=[key])
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_conflicting_real_variants_cannot_replace_last_good(self):
        from botend.models import WowItemVariantSnapshot
        # This gate concerns conflicting numeric facts, not independently valid
        # public effects (which may now be published with unknown stats).
        WowItemVariantSnapshot.objects.filter(item__item_id=60).update(effects_json=[])
        key = self.target()
        snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        path = snapshots._directory(key) / 'data.json'
        previous = path.read_bytes()
        variant = WowItemVariantSnapshot.objects.select_related('item').get(item__item_id=60)
        original = variant
        from copy import deepcopy
        variant = deepcopy(original)
        variant.pk = None
        variant.variant_key = 'conflicting-unit-fact'
        variant.item_level += 1
        variant.save()
        # Prove both rows belong to the exact central difficulty coordinate;
        # an ignored branch/build fixture would not test conflict rejection.
        from botend.services.wow_item_display import _difficulty_variant, load_item_tooltip_metadata
        from botend.portal.adventure_journal import difficulty_item_context
        context = dict(difficulty_id=key['difficulty'],
            item_context=difficulty_item_context(self.release.manifest['catalog']['difficulties'], key['difficulty']),
            instance_id=key['instance_id'], difficulty_rule_build=key['source']['build'])
        self.assertIs(_difficulty_variant([original], context, 60), original)
        self.assertIs(_difficulty_variant([variant], context, 60), variant)
        self.assertIsNone(_difficulty_variant([original, variant], context, 60))
        self.assertEqual(load_item_tooltip_metadata([dict(context, item_id=60,
            data_branch=original.data_branch)])[0]['difficulty_status'], 'unresolved')
        with self.assertLogs(snapshots.logger, level='ERROR'), self.assertRaises(RuntimeError):
            snapshots.refresh_journal_loot_snapshots(strict=True, force=True, targets=[key])
        self.assertEqual(path.read_bytes(), previous)

    def test_each_difficulty_validates_its_own_item_count(self):
        from botend.tests.journal_snapshot_fixtures import seed_snapshot_difficulty_facts, warm_journal
        boss = self.instance.encounters.get()
        boss.payload['loot'][1]['difficulty_ids'] = [1, 2]
        boss.save(update_fields=['payload'])
        seed_snapshot_difficulty_facts(self.release)
        warm_journal(10)
        self.assertEqual(self.read(1)['coverage']['expected_count'], 2)
        self.assertEqual(self.read(2)['coverage']['expected_count'], 1)

    def test_source_property_forms_support_armor_and_weapon_without_fake_stats(self):
        from copy import deepcopy
        key = self.target()
        snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        original = snapshots._read_projection(key)
        for properties in ({'armor': 120}, {'weapon_damage': {'min': 10, 'max': 20}},
                           {'raw_stats': {'stamina': 20}}, {'effects': ['Equip: unit effect']}):
            with self.subTest(properties=properties):
                data = deepcopy(original)
                data['loot'][0]['details'].update(stats=[], effects=[], **{
                    field: value for field, value in properties.items() if field != 'effects'})
                if 'effects' in properties:
                    data['loot'][0]['details']['effects'] = properties['effects']
                snapshots._validate_coverage(data, key, data['coverage']['expected'])

    def test_strict_poll_cannot_fast_skip_unverified_ready_even_recent(self):
        from botend.services.gear_catalog_snapshot import _source
        key = self.target()
        snapshots._write(snapshots._directory(key) / 'data.json', {
            'coordinate': key, 'snapshot': {'state': 'ready'}, 'loot': [], 'loot_total': 0,
            'source_version': _source()[2], 'built_at': snapshots.time.time()})
        snapshots._write(snapshots.snapshot_root() / 'source-check.json', {
            'at': snapshots.time.time(), 'revision': {}, 'pending': False})
        with patch('botend.portal.adventure_journal.build_loot_projection', return_value={'loot': [], 'loot_total': 0}):
            with self.assertLogs(snapshots.logger, level='ERROR'), self.assertRaises(RuntimeError):
                snapshots.refresh_journal_loot_snapshots(strict=True, poll=True, targets=[key])

    def test_business_field_corruption_is_rejected_on_file_read(self):
        from copy import deepcopy
        key = self.target()
        snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        path = snapshots._directory(key) / 'data.json'
        original = snapshots._load(path)
        changes = [dict(status='basic'), dict(complete=False), dict(item_level=True),
                   dict(item_level=0), dict(quality=True), dict(quality=2),
                   dict(stats=[], effects=[]), dict(stats=['  '], effects=[])]
        for changeset in changes:
            with self.subTest(changeset=changeset):
                data = deepcopy(original)
                data['loot'][0]['details'].update(changeset)
                snapshots._write(path, data)
                self.assertIsNone(snapshots._read_projection(key))
        for field, value in [('expected_count', 0), ('actual_count', 0), ('equipment_count', 0),
                             ('frozen_identity', {}), ('complete', False)]:
            data = deepcopy(original)
            data['coverage'][field] = value
            snapshots._write(path, data)
            self.assertIsNone(snapshots._read_projection(key))
        data = deepcopy(original)
        data['loot'][0]['sources'] = []
        snapshots._write(path, data)
        self.assertIsNone(snapshots._read_projection(key))

    def test_one_invalid_of_two_items_fails_full_coverage_not_percentage(self):
        from copy import deepcopy
        from botend.tests.journal_snapshot_fixtures import seed_snapshot_difficulty_facts
        boss = self.instance.encounters.get()
        drop = deepcopy(boss.payload['loot'][1])
        drop['difficulty_ids'] = [1]
        boss.payload['loot'][1] = drop
        boss.save(update_fields=['payload'])
        seed_snapshot_difficulty_facts(self.release)
        from botend.portal.adventure_journal import build_loot_projection
        from botend.services.journal_snapshot import loot_version
        key = snapshots.coordinate(self.release.id, 10, 1, self.source,
            journal_version=loot_version([boss], 1))
        snapshots.read_loot_projection(self.release.id, 10, 1, self.source,
            journal_version=key['journal_version'])
        def partial(*args):
            result = build_loot_projection(*args)
            self.assertEqual(len(result['loot']), 2)
            result['loot'][1]['details'].update(status='unresolved', complete=False)
            return result
        with patch('botend.portal.adventure_journal.build_loot_projection', side_effect=partial):
            with self.assertLogs(snapshots.logger, level='ERROR'), self.assertRaises(RuntimeError):
                snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        self.assertFalse((snapshots._directory(key) / 'data.json').exists())

    def test_authoritative_empty_scope_is_ready_only_with_zero_matching_set(self):
        boss = self.instance.encounters.get()
        boss.payload['loot'] = []
        boss.save(update_fields=['payload'])
        key = snapshots.coordinate(self.release.id, 10, 1, self.source)
        snapshots.read_loot_projection(self.release.id, 10, 1, self.source)
        snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        data = snapshots._read_projection(key)
        self.assertEqual((data['loot_total'], data['coverage']['expected_count'], data['loot']), (0, 0, []))

    def test_strict_readback_checks_business_not_just_ready_marker(self):
        key = self.target()
        write = snapshots._write
        def corrupt(path, payload):
            if path.name == 'data.json':
                payload['loot'][0]['details']['item_level'] = None
            write(path, payload)
        with patch.object(snapshots, '_write', side_effect=corrupt), self.assertRaises(RuntimeError):
            snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])

    def test_ready_marker_without_business_proof_is_not_readable(self):
        self.read()
        key = snapshots._load(next(snapshots.snapshot_root().glob('*/request.json')))
        snapshots._write(snapshots._directory(key) / 'data.json', {
            'coordinate': key, 'snapshot': {'state': 'ready'}, 'loot': [], 'loot_total': 0})
        self.assertIsNone(snapshots._read_projection(key))
