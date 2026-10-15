"""Closed synthetic Journal scope; actual DB2 graph/resolver/parser/ORM, no network."""
from copy import deepcopy
from io import StringIO
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from botend.journal_models import JournalRelease, JournalState, JournalInstance, JournalEncounter
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.management.commands import import_journal_difficulty_variants as command
from botend.portal.adventure_journal import instance_source
from botend.services.journal_snapshot import _source
from botend.services.wow_item_difficulty_import import import_difficulty_variant
from botend.services.wow_item_difficulty_import import _digest
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts
from botend.tests.test_wow_item_fixed_cosmetic import FIXTURES, resolve


class JournalDifficultyImportCommandTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest_path = Path(self.directory.name) / 'manifest.json'
        self.proof_path = Path(self.directory.name) / 'proofs.jsonl'
        self.season = SeasonMeta.objects.create(season_key='closed-unit', season_name='Unit',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='unit-batch')
        self.item = WowItemSnapshot.objects.create(item_id=258045, name='Unit base', quality=3,
            catalog_type='equipment', slot_key='main_hand', inventory_type=13, item_class_id=2)
        self.release = JournalRelease.objects.create(build='12.1.0.69587', status='completed',
            completed_at=timezone.now(), report={'instances':1, 'encounters':1, 'loot':1},
            manifest={'catalog': {'tiers': [], 'difficulties': [{'id':1, 'context':1}, {'id':2, 'context':2}]}})
        JournalState.objects.create(pk='wow-zhCN', active_release=self.release)
        self.instance = JournalInstance.objects.create(release=self.release, journal_id=1304,
            name='Closed synthetic instance', kind='dungeon', expansion=1200,
            payload=dict(id=1304, name='Closed synthetic instance', kind='dungeon', expansion=1200, difficulty_ids=[1,2]))
        self.drop = dict(item_id=258045, class_id=2, slot=13, quality=3, difficulty_ids=[1,2])
        self.boss = JournalEncounter.objects.create(instance=self.instance, journal_id=1, name='Unit boss',
            payload=dict(difficulty_ids=[1,2], loot=[self.drop]))
        source = instance_source(self.release, 1304, season=self.season)
        season = dict(id=self.season.pk, key=self.season.season_key, batch=self.season.gear_batch_key)
        self.targets, self.proofs = [], []
        # Uses captured public tooltip and real fixed-cosmetic DB2 fixture.
        for difficulty in (1,2):
            rule = resolve(difficulty)
            rule.update(data_branch='retail', difficulty_id=difficulty, instance_id=1304)
            kw = dict(rule=rule,
                payload=json.loads((FIXTURES / 'wow_item_fixed_cosmetic_258045_tooltip_zhcn.json').read_text()),
                tooltip_evidence=dict(identity_validated=True, item_id=258045, data_branch='retail',
                    game_build='', url='https://nether.wowhead.com/tooltip/item/258045?locale=zhcn&dataEnv=1'))
            target = dict(instance_id=1304, instance_name=self.instance.name, expansion=1200,
                difficulty_id=difficulty, item_context=difficulty, item_id=258045,
                source=source, class_id=2, slot=13, base_quality=3)
            self.targets.append(target)
            self.proofs.append(dict(target, release_id=self.release.pk, season=season, status='verified',
                **{k:kw[k] for k in ('rule','payload','tooltip_evidence')}))
        self.manifest = dict(release=dict(id=self.release.pk, build=self.release.build,
            updated=self.release.completed_at.isoformat(), counts=self.release.report), source_version=_source()[2],
            season=season, instances=[dict(self.instance.payload, source=source)], targets=self.targets)
        self.save()

    def save(self):
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.proof_path.write_text(''.join(json.dumps(row)+'\n' for row in self.proofs))

    def run_command(self, apply=False, **kwargs):
        output = StringIO()
        if apply:
            kwargs.update(apply=True, confirm_manifest_sha256=hashlib.sha256(self.manifest_path.read_bytes()).hexdigest())
        call_command('import_journal_difficulty_variants', manifest=str(self.manifest_path),
            proofs=str(self.proof_path), stdout=output, **kwargs)
        return json.loads(output.getvalue())

    def test_nongear_manifest_targets_are_validated_but_not_imported(self):
        nongear = dict(self.targets[0], item_id=999001, class_id=15, slot=0, base_quality=3)
        self.boss.payload['loot'].append(dict(item_id=999001, class_id=15, slot=0, quality=3, difficulty_ids=[1]))
        self.boss.save(update_fields=['payload'])
        self.manifest['targets'].append(nongear)
        self.save()
        result = self.run_command()
        self.assertEqual(result['expected_count'], 2)
        self.assertEqual(result['skipped_nongear_count'], 1)
        self.manifest['targets'][-1]['class_id'] = 2
        self.save()
        with self.assertRaises(CommandError):
            self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_default_dry_no_writes_and_real_tooltip_complete_scope(self):
        before = list(WowItemSnapshot.objects.values())
        with CaptureQueriesContext(connection) as queries:
            result = self.run_command()
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries),
                        [q['sql'] for q in queries if not q['sql'].lstrip().upper().startswith('SELECT')])
        self.assertEqual(result['expected_count'], 2)
        self.assertEqual(result['selected_instance_count'], 1)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['created'], 0)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())
        self.assertEqual(before, list(WowItemSnapshot.objects.values()))

    def test_explicit_apply_hash_and_idempotency(self):
        for value in (None, '0'*64, hashlib.sha256(self.manifest_path.read_bytes()).hexdigest().upper()):
            with self.assertRaises(CommandError):
                call_command(
                    'import_journal_difficulty_variants', manifest=str(self.manifest_path), proofs=str(self.proof_path),
                    apply=True, confirm_manifest_sha256=value, stdout=StringIO())
        first = self.run_command(apply=True)
        self.assertEqual(first['created'], 2)
        rows = list(WowItemVariantSnapshot.objects.values())
        self.assertEqual(self.run_command(apply=True)['noop'], 2)
        self.assertEqual(rows, list(WowItemVariantSnapshot.objects.values()))
        for row in rows:
            self.assertEqual(row['stats_json'], dict(min_damage=1,max_damage=1,weapon_speed=3.0,weapon_dps=0.3))

    def test_fake_subset_and_all_selected_difficulties_complete(self):
        self.manifest['targets'] = self.targets[:1]
        self.proofs = self.proofs[:1]
        self.save()
        with self.assertRaisesMessage(CommandError, 'actual scope mismatch'):
            self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_missing_proof_never_writes(self):
        self.proofs.pop()
        self.save()
        with self.assertRaisesMessage(CommandError, 'missing verified proofs'):
            self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_unknown_instance_and_strict_integer(self):
        for selection in ([999], [True], ['1304']):
            with self.subTest(selection=selection), self.assertRaises(CommandError):
                self.run_command(instance=selection)
        self.assertEqual(self.run_command(instance=[1304])['expected_count'], 2)
        for value in ('01304', '+1304', ' 1304', '1.0', 'True'):
            with self.assertRaises(Exception):
                command.strict_instance(value)

    def test_source_branch_mismatch(self):
        self.proofs[0]['source'] = {**self.proofs[0]['source'], 'data_branch':'ptr'}
        self.save()
        with self.assertRaisesMessage(CommandError, 'source branch mismatch'):
            self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_conflicting_verified_dedup_and_unresolved_retry(self):
        unresolved = dict(self.proofs[0], status='unresolved')
        self.proofs += [deepcopy(self.proofs[0]), unresolved]
        self.save()
        self.assertEqual(self.run_command()['validated_count'], 2)
        conflict = deepcopy(self.proofs[0]); conflict['payload']['quality'] = 4
        self.proofs.append(conflict); self.save()
        with self.assertRaisesMessage(CommandError, 'conflicting verified'):
            self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_changed_season_before_first_write(self):
        original = command.load_proofs
        def change(*args):
            result = original(*args)
            SeasonMeta.objects.filter(pk=self.season.pk).update(gear_batch_key='changed')
            return result
        with patch.object(command, 'load_proofs', side_effect=change):
            with self.assertRaisesMessage(CommandError, 'active season/batch changed'):
                self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_batch_and_release_race_roll_back_entire_batch(self):
        for race in ('batch', 'release'):
            calls = []
            def injection(**kwargs):
                result = import_difficulty_variant(**kwargs)
                if kwargs.get('apply'):
                    calls.append(1)
                    if len(calls) == 1:
                        if race == 'batch':
                            SeasonMeta.objects.filter(pk=self.season.pk).update(gear_batch_key='race')
                        else:
                            JournalState.objects.filter(pk='wow-zhCN').update(active_release=None)
                return result
            with self.subTest(race=race), patch.object(command, 'import_difficulty_variant', side_effect=injection):
                with self.assertRaises(CommandError):
                    self.run_command(apply=True)
            self.assertFalse(WowItemVariantSnapshot.objects.exists())
            self.season.refresh_from_db()
            self.assertEqual(self.season.gear_batch_key, 'unit-batch')
            self.assertEqual(JournalState.objects.get(pk='wow-zhCN').active_release_id, self.release.pk)

    def test_last_invalid_parser_proof_prevents_first_write(self):
        self.proofs[-1]['payload']['tooltip'] = '<!--ilvl-->1'
        self.save()
        with self.assertRaises(CommandError):
            self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_bool_counts_and_header_source_identity_rejected(self):
        self.manifest['release']['counts']['instances'] = True
        self.save()
        with self.assertRaises(CommandError):
            self.run_command()

    def test_file_bounds(self):
        with patch.object(command, 'MAX_FILE_BYTES', 10):
            with self.assertRaisesMessage(CommandError, 'size limit'):
                self.run_command()
        with patch.object(command, 'MAX_LINE_BYTES', 10):
            with self.assertRaisesMessage(CommandError, 'line/count limit'):
                self.run_command()
        with patch.object(command, 'MAX_PROOF_LINES', 1):
            with self.assertRaisesMessage(CommandError, 'line/count limit'):
                self.run_command()

    def test_manifest_mutation_after_preflight_refused(self):
        original = command.load_proofs
        def mutate(*args):
            result = original(*args)
            self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b'\n')
            return result
        with patch.object(command, 'load_proofs', side_effect=mutate):
            with self.assertRaisesMessage(CommandError, 'manifest file changed'):
                self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_default_means_all_actual_instances_not_only_collected(self):
        payload = dict(self.instance.payload, id=1305, name='Uncollected synthetic instance')
        second = JournalInstance.objects.create(release=self.release, journal_id=1305,
            name=payload['name'], kind='dungeon', expansion=1200, payload=payload)
        JournalEncounter.objects.create(instance=second, journal_id=2, name='Other unit boss', payload=self.boss.payload)
        source = instance_source(self.release, 1305, season=self.season)
        self.manifest['instances'].append(dict(payload, source=source))
        self.manifest['targets'] += [dict(t, instance_id=1305, instance_name=second.name, source=source) for t in self.targets[:2]]
        self.save()
        self.assertEqual(self.run_command(instance=[1304])['expected_count'], 2)
        with self.assertRaisesMessage(CommandError, 'missing verified proofs: 2 of 4'):
            self.run_command(apply=True)
        self.assertFalse(WowItemVariantSnapshot.objects.exists())

    def test_real_62_envelope_scaling_graph_through_command(self):
        # Only the tooltip here is explicitly synthetic; DB2 graph is captured.
        envelopes = json.loads((FIXTURES / 'wow_item_difficulty_item250243_69587.json').read_text())
        self.assertTrue(any(e["table"] == "ItemBonusTree" for e in envelopes))
        for envelope in envelopes:
            envelope['provider'] = 'wago'
            envelope['source_sha256'] = _digest({k:envelope[k] for k in ('build','table','filter','rows','total')})
        records = {(e['table'], *next(iter(e['filter'].items()))):e for e in envelopes}
        rule = resolve_difficulty_bonus_facts(250243, 1, self.release.build, tables=lambda t,f,k:records.get((t,f,k)))
        rule.update(data_branch='retail', difficulty_id=1, instance_id=1304)
        WowItemSnapshot.objects.create(item_id=250243, catalog_type='equipment', slot_key='head')
        self.boss.payload['loot'].append(dict(item_id=250243, class_id=4, slot=1, quality=3, difficulty_ids=[1]))
        self.boss.save(update_fields=['payload'])
        target = dict(self.targets[0], item_id=250243, class_id=4, slot=1)
        self.manifest['targets'].append(target)
        self.proofs.append(dict(target, release_id=self.release.pk, season=self.manifest['season'], status='verified',
            rule=rule, payload=dict(id=250243, quality=3, itemLevel=214,
                tooltip='<!--ilvl-->214<br><!--rf-->+12 Intellect<!--nameDescStats-->'),
            tooltip_evidence=dict(identity_validated=True, item_id=250243, data_branch='retail', game_build='',
                url='https://nether.wowhead.com/tooltip/item/250243?bonus=13442:13437&dataEnv=1')))
        self.save()
        self.assertEqual(self.run_command(apply=True)['created'], 3)
        self.assertEqual(WowItemVariantSnapshot.objects.get(item__item_id=250243).stats_json, {'intellect':12})
