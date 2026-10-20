"""Offline captured-source replay. Catalog stays outside the repository."""
import copy
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone
from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import SeasonMeta, PlayerSpecTopPlayer, RaiderIOCharacterActivity
from botend.services.player_raiderio_activity import activity_fields, player_identity, project_player_activity


class RaidProgressionTests(TestCase):
    def setUp(self):
        root = os.environ.get('RAID_PROGRESS_FIXTURES')
        if root:
            root = Path(root)
            self.central = json.loads((root / 'central.json').read_text())
            self.catalog = json.loads((root / 'catalog.json').read_text())
            self.data = json.loads((root / 'explicit.json').read_text())['data']
            self.mapping = json.loads((root / 'journal-mapping.json').read_text())
        else:
            fixture = json.loads((Path(__file__).parent / 'fixtures/physical_raid_progression_capture.json').read_text())
            self.central, self.catalog, self.data, self.mapping = (fixture[k] for k in ('central', 'catalog', 'data', 'mapping'))
        self.season = SeasonMeta.objects.create(**self.central['season'], is_active=True, mplus_zone_id=55)
        self.release = JournalRelease.objects.create(build='captured-current', status='ready')
        JournalState.objects.create(active_release=self.release)
        instances = {}
        for bid, iid, name in self.mapping:
            instance = instances.setdefault(iid, None)
            if instance is None:
                instance = instances[iid] = JournalInstance.objects.create(release=self.release, journal_id=iid, name=name, kind='raid')
            JournalEncounter.objects.create(instance=instance, journal_id=bid, name=str(bid), payload={'dungeon_encounter_id': bid})
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='cn', realm='Silver Hand',
            character_name='Monosuper', profile_url=self.data['profile_url'], class_name='DeathKnight',
            spec_name='Unholy', rank=1, last_updated=timezone.now())
        self.row = RaiderIOCharacterActivity.objects.create(season=self.season, **player_identity(self.player),
            payload_json={'raid_progression': copy.deepcopy(self.data['raid_progression'])})

    def bind(self):
        from botend.services.player_raid_progression import bind_catalog
        bind_catalog(self.season.pk, self.catalog['data'], self.catalog['url'], apply=True)
        self.season.refresh_from_db()

    def test_captured_three_physical_raids_use_central_names(self):
        # RED: current projection leaves the two physical miniraids as English slugs.
        rows = project_player_activity(self.player)['raid_progression']['rows']
        self.assertCountEqual([r['name'] for r in rows], sorted({r[2] for r in self.mapping}))

    def test_bound_capture_parser_orm_projection_and_miniraids(self):
        from botend.services.player_raiderio_activity import ActivityCollector
        from botend.services.player_raid_progression import REFERENCE, bound_scope
        self.bind()
        refs = self.season.raid_zones[0][REFERENCE]
        self.assertEqual(len(refs['bindings']), 3)
        self.assertTrue(all(set(r) == {'slug', 'journal_id'} for r in refs['bindings']))
        self.row.payload_json = {}; self.row.save()
        response = Mock(status_code=200, headers={}, json=lambda: copy.deepcopy(self.data))
        transport = Mock(return_value=response)
        with patch('requests.get', side_effect=AssertionError('offline')):
            result = ActivityCollector(transport=transport).refresh(self.player, self.season, include_profile=True)
            view = project_player_activity(self.player)
        self.assertEqual(result['profile_payload'], self.data)
        self.assertEqual(transport.call_count, 1)
        self.assertNotIn('current-tier', transport.call_args.kwargs['params']['fields'])
        rows = view['raid_progression']['rows']
        self.assertEqual([r['name'] for r in rows], ['烈毒之渊', '潮缚石窟', '基希克斯破封'])
        self.assertEqual([r['total_bosses'] for r in rows], [8, 1, 1])
        self.assertEqual([r['mythic'] for r in rows], [8, 1, 0])
        self.row.refresh_from_db()
        self.assertEqual(self.row.payload_json['raid_progression'], self.data['raid_progression'])

    def test_missing_stale_ambiguous_mapping_fails_closed_and_monitor_preserves_only_same_scope(self):
        from botend.services.player_raid_progression import bound_scope, preserve_references, REFERENCE
        from botend.services.player_raiderio_activity import ActivityCollector
        transport = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: self.data))
        ActivityCollector(transport=transport).refresh(self.player, self.season)
        self.assertNotIn('raid_progression', transport.call_args.kwargs['params']['fields'])
        block = project_player_activity(self.player)['raid_progression']
        self.assertEqual(block['status'], 'unresolved')
        self.assertTrue(all(r['mythic'] is None for r in block['rows']))
        self.bind()
        bare = copy.deepcopy(self.central['season']['raid_zones'])
        kept = preserve_references(self.season, bare, self.season.raid_encounters, self.season.rio_season)
        self.assertEqual(kept, self.season.raid_zones)
        self.assertNotIn(REFERENCE, preserve_references(self.season, bare, self.season.raid_encounters[:-1], self.season.rio_season)[0])
        new = JournalRelease.objects.create(build='next', status='ready')
        for instance in JournalInstance.objects.filter(release=self.release):
            other = JournalInstance.objects.create(release=new, journal_id=instance.journal_id, name=instance.name, kind='raid')
            for e in instance.encounters.all():
                JournalEncounter.objects.create(instance=other, journal_id=e.journal_id, name=e.name, payload=e.payload)
        JournalState.objects.update(active_release=new)
        with self.assertRaises(ValueError):
            bound_scope(self.season)
        self.assertEqual(project_player_activity(self.player)['raid_progression']['status'], 'unresolved')
        self.bind()
        first = JournalInstance.objects.filter(release=new).first()
        JournalEncounter.objects.create(instance=first, journal_id=999999, name='duplicate', payload={'dungeon_encounter_id': 3513})
        with self.assertRaises(ValueError):
            self.bind()

    def test_zero_missing_unknown_raid_and_malformed_preserve_valid_facts(self):
        from botend.services.player_raiderio_activity import ActivityCollector, validate_payload
        self.bind()
        data = copy.deepcopy(self.data)
        data['raid_progression'].pop('the-tidebound-grotto')
        data['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'] = None
        data['raid_progression']['old-season-raid'] = {'total_bosses': 99}
        transport = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: data))
        ActivityCollector(transport=transport).refresh(self.player, self.season)
        self.row.refresh_from_db()
        self.assertEqual(self.row.payload_json['raid_progression']['the-tidebound-grotto'], self.data['raid_progression']['the-tidebound-grotto'])
        self.assertNotIn('old-season-raid', self.row.payload_json['raid_progression'])
        self.assertEqual(self.row.payload_json['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'], 0)
        bads = []
        bad = copy.deepcopy(data); bad['name'] = 'wrong'; bad['profile_url'] = bad['profile_url'].replace('Monosuper', 'wrong'); bads.append(bad)
        bad = copy.deepcopy(data); bad['mythic_plus_scores_by_season'][0]['season'] = 'old'; bads.append(bad)
        for value in (True, -1, 2, '1'):
            bad = copy.deepcopy(data); bad['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'] = value; bads.append(bad)
        for bad in bads:
            with self.assertRaises(ValueError):
                validate_payload(bad, player_identity(self.player), self.season)
        self.row.payload_json = {}; self.row.save()
        rows = project_player_activity(self.player)['raid_progression']['rows']
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['mythic'] is None for r in rows))
        from django.template.loader import render_to_string
        html = render_to_string('portal/spec_detail/_player_raiderio_activity.html', {'activity': project_player_activity(self.player)})
        # The template receives its public context through the parent include.
        self.assertNotIn('0/8', html)

    def test_raid_only_default_dry_run_clocks_and_actual_ai_semantic_reuse(self):
        from botend.services.player_raid_progression import refresh_raid_progression, META
        from botend.services.player_observation import build_evidence, _same_publication, PROMPT_VERSION
        from types import SimpleNamespace
        self.bind()
        now = timezone.now()
        from botend.services.player_raiderio_activity import _date
        self.row.status = 'ready'; self.row.fetched_at = now
        self.row.source_updated_at = _date(self.data.get('last_crawled_at'))
        self.row.next_attempt_at = now
        self.row.module_fetched_at = {'mythic_plus_best_runs': now.isoformat(), 'raid_progression': now.isoformat()}
        self.row.payload_json.update(raid_first_kills={'sentinel': 'untouched'}, mythic_plus_recent_runs=[], mythic_plus_best_runs=[])
        self.row.save()
        before = RaiderIOCharacterActivity.objects.values().get(pk=self.row.pk)
        before_player = PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk)
        evidence = build_evidence(self.player)
        self.assertIsNotNone(evidence)
        publication = SimpleNamespace(evidence_hash=evidence['evidence_hash'], evidence_json=copy.deepcopy(evidence), prompt_version=PROMPT_VERSION)
        transport = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: self.data))
        self.assertEqual(refresh_raid_progression(self.player, transport=transport)['status'], 'dry_run')
        transport.assert_not_called()
        self.assertEqual(RaiderIOCharacterActivity.objects.values().get(pk=self.row.pk), before)
        self.assertEqual(refresh_raid_progression(self.player, transport=transport, apply=True)['status'], 'ready')
        self.assertEqual(transport.call_count, 1)
        after = RaiderIOCharacterActivity.objects.values().get(pk=self.row.pk)
        for key in before:
            if key not in ('payload_json', 'module_fetched_at'):
                self.assertEqual(before[key], after[key], key)
        self.assertEqual(before_player, PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk))
        for key in before['payload_json']:
            if key != 'raid_progression':
                self.assertEqual(before['payload_json'][key], after['payload_json'][key])
        after_evidence = build_evidence(self.player)
        self.assertNotEqual(evidence['evidence_hash'], after_evidence['evidence_hash'])
        self.assertTrue(_same_publication(publication, after_evidence))

    def test_raid_only_failure_429_lease_and_full_request_race(self):
        from botend.services.player_raid_progression import refresh_raid_progression, META
        from botend.services.player_raiderio_activity import ActivityCollector
        self.bind()
        old = copy.deepcopy(self.row.payload_json)
        limited = Mock(return_value=Mock(status_code=429, headers={'Retry-After': '600'}))
        self.assertEqual(refresh_raid_progression(self.player, transport=limited, apply=True)['status'], 'rate_limited')
        self.assertEqual(refresh_raid_progression(self.player, transport=limited, apply=True)['status'], 'backoff')
        self.assertEqual(limited.call_count, 1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.payload_json['raid_progression'], old['raid_progression'])
        self.row.payload_json.pop(META); self.row.save()
        new = copy.deepcopy(self.data)
        new['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'] = 1
        def full_network(*args, **kwargs):
            raid_transport = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: new))
            self.assertEqual(refresh_raid_progression(self.player, transport=raid_transport, apply=True)['status'], 'ready')
            # Another independent module writes while the original request is in flight.
            current = RaiderIOCharacterActivity.objects.get(pk=self.row.pk)
            current.payload_json['unrelated'] = {'new': True}; current.save()
            return Mock(status_code=200, headers={}, json=lambda: self.data)
        result = ActivityCollector(transport=full_network).refresh(self.player, self.season, force=True, include_profile=True)
        self.assertEqual(result['profile_payload'], self.data)
        self.row.refresh_from_db()
        self.assertEqual(self.row.payload_json['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'], 1)
        self.assertEqual(self.row.payload_json['unrelated'], {'new': True})
        # Opposite ordering: a full-profile publication fences the old raid-only response.
        self.row.payload_json.pop(META); self.row.save()
        def raid_network(*args, **kwargs):
            collector = ActivityCollector(transport=Mock(return_value=Mock(status_code=200, headers={}, json=lambda: new)))
            collector.refresh(self.player, self.season, force=True)
            return Mock(status_code=200, headers={}, json=lambda: self.data)
        self.assertEqual(refresh_raid_progression(self.player, transport=raid_network, apply=True)['status'], 'superseded')
        self.row.refresh_from_db()
        self.assertEqual(self.row.payload_json['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'], 1)

    def test_command_dry_run_exact_player_and_invalid_sources_preserve_last_good(self):
        import io
        from django.core.management import call_command
        from botend.services.player_raid_progression import bind_catalog, refresh_raid_progression, META
        before = copy.deepcopy(self.season.raid_zones)
        refs = bind_catalog(self.season.pk, self.catalog['data'], self.catalog['url'])
        self.season.refresh_from_db()
        self.assertEqual(self.season.raid_zones, before)
        self.assertEqual(len(refs['bindings']), 3)
        self.bind()
        good = copy.deepcopy(self.season.raid_zones)
        broken = copy.deepcopy(self.catalog['data'])
        broken['raids'] = [r for r in broken['raids'] if r['slug'] != 'the-tidebound-grotto']
        with self.assertRaises(ValueError):
            bind_catalog(self.season.pk, broken, self.catalog['url'], apply=True)
        self.season.refresh_from_db()
        self.assertEqual(self.season.raid_zones, good)
        out = io.StringIO()
        with patch('requests.get', side_effect=AssertionError('dry run must not fetch')):
            call_command('refresh_player_raid_progression', player_id=self.player.pk, stdout=out)
        self.assertEqual(json.loads(out.getvalue())['status'], 'dry_run')
        for mutation in ('identity', 'missing', 'malformed'):
            data = copy.deepcopy(self.data)
            if mutation == 'identity':
                data['profile_url'] = data['profile_url'].replace('Monosuper', 'Other')
            elif mutation == 'missing':
                data.pop('raid_progression')
            else:
                data['raid_progression']['the-tidebound-grotto'] = 'invalid'
            self.row.refresh_from_db()
            self.row.payload_json.pop(META, None); self.row.save()
            previous = copy.deepcopy(self.row.payload_json)
            result = refresh_raid_progression(self.player, apply=True,
                transport=Mock(return_value=Mock(status_code=200, headers={}, json=lambda: data)))
            self.assertEqual(result['status'], 'invalid_payload')
            self.row.refresh_from_db()
            self.assertEqual(self.row.payload_json['raid_progression'], previous['raid_progression'])

    def test_raid_only_busy_and_expired_lease_cas(self):
        from botend.services.player_raid_progression import refresh_raid_progression, META
        from django.db import connection
        from datetime import timedelta
        self.bind()
        depth = len(connection.atomic_blocks)
        newer = copy.deepcopy(self.data)
        newer['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'] = 1
        def network(*args, **kwargs):
            self.assertEqual(len(connection.atomic_blocks), depth)
            unused = Mock(side_effect=AssertionError('busy must not fetch'))
            self.assertEqual(refresh_raid_progression(self.player, apply=True, transport=unused)['status'], 'busy')
            current = RaiderIOCharacterActivity.objects.get(pk=self.row.pk)
            current.payload_json[META]['next_attempt_at'] = (timezone.now() - timedelta(seconds=1)).isoformat()
            current.save()
            response = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: newer))
            self.assertEqual(refresh_raid_progression(self.player, apply=True, transport=response)['status'], 'ready')
            return Mock(status_code=200, headers={}, json=lambda: self.data)
        self.assertEqual(refresh_raid_progression(self.player, apply=True, transport=network)['status'], 'superseded')
        self.row.refresh_from_db()
        self.assertEqual(self.row.payload_json['raid_progression']['the-unbinding-of-kithix']['mythic_bosses_killed'], 1)

    def test_source_clock_fences_sequential_full_raid_full_and_allows_correction(self):
        from botend.services.player_raiderio_activity import ActivityCollector, _date
        from botend.services.player_raid_progression import refresh_raid_progression, META
        self.bind()
        slug = 'the-unbinding-of-kithix'
        def publish(day, kills, full=False):
            data = copy.deepcopy(self.data)
            data['last_crawled_at'] = f'2026-10-{day}T00:00:00Z' if day else None
            data['raid_progression'][slug]['mythic_bosses_killed'] = kills
            for field in ('mythic_plus_best_runs', 'mythic_plus_recent_runs', 'mythic_plus_dungeon_run_counts'):
                data[field] = []
            self.row.refresh_from_db()
            meta = self.row.payload_json.get(META, {})
            meta.pop('next_attempt_at', None)
            self.row.save()
            transport = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: data))
            if full:
                result = ActivityCollector(transport=transport).refresh(self.player, self.season, force=True, include_profile=True)
            else:
                result = refresh_raid_progression(self.player, apply=True, transport=transport)
            self.row.refresh_from_db()
            return result
        publish('20', 1, full=True)
        public_clock = self.row.source_updated_at
        raid_clock = self.row.module_fetched_at['raid_progression']
        publish('19', 0)
        self.assertEqual(self.row.payload_json['raid_progression'][slug]['mythic_bosses_killed'], 1)
        self.assertEqual(self.row.module_fetched_at['raid_progression'], raid_clock)
        self.assertEqual(self.row.source_updated_at, public_clock)
        self.assertEqual(_date(self.row.payload_json[META]['source_updated_at']), public_clock)
        publish('21', 0)  # New source may correct DOWN; max(kills) is not freshness.
        self.assertEqual(self.row.payload_json['raid_progression'][slug]['mythic_bosses_killed'], 0)
        raid_clock = self.row.module_fetched_at['raid_progression']
        publish('20', 1, full=True)
        self.assertEqual(self.row.payload_json['raid_progression'][slug]['mythic_bosses_killed'], 0)
        self.assertEqual(self.row.module_fetched_at['raid_progression'], raid_clock)
        self.assertEqual(self.row.payload_json['mythic_plus_best_runs'], [])
        publish(None, 1, full=True)
        self.assertEqual(self.row.payload_json['raid_progression'][slug]['mythic_bosses_killed'], 0)
        self.assertEqual(_date(self.row.payload_json[META]['source_updated_at']), _date('2026-10-21T00:00:00Z'))
        publish(None, 1)
        self.assertEqual(self.row.module_fetched_at['raid_progression'], raid_clock)
        # Legacy rows use a global source clock only when they already store raid facts.
        self.row.payload_json.pop(META)
        self.row.source_updated_at = _date('2026-10-22T00:00:00Z'); self.row.save()
        publish('19', 1, full=True)
        self.assertEqual(_date(self.row.payload_json[META]['source_updated_at']), _date('2026-10-22T00:00:00Z'))
        publish('20', 1)
        self.assertEqual(self.row.payload_json['raid_progression'][slug]['mythic_bosses_killed'], 0)
        self.row.payload_json = {}; self.row.module_fetched_at = {}; self.row.save()
        publish(None, 1)
        self.assertEqual(self.row.payload_json['raid_progression'][slug]['mythic_bosses_killed'], 1)
        self.assertIsNone(self.row.payload_json[META].get('source_updated_at'))

    def test_partial_response_is_not_fresh_even_when_history_fills_gaps(self):
        from datetime import timedelta
        from botend.services.player_raiderio_activity import ActivityCollector, _date
        from botend.services.player_raid_progression import refresh_raid_progression, META
        self.bind()
        slug = 'the-unbinding-of-kithix'
        for full in (False, True):
            for cached in (False, True):
                with self.subTest(full=full, cached=cached):
                    self.row.payload_json = {'raid_progression': copy.deepcopy(self.data['raid_progression'])} if cached else {}
                    self.row.module_fetched_at = {}; self.row.source_updated_at = None; self.row.save()
                    data = copy.deepcopy(self.data)
                    data['raid_progression'] = {slug: {'total_bosses': 1, 'mythic_bosses_killed': None}}
                    for field in ('mythic_plus_best_runs', 'mythic_plus_recent_runs', 'mythic_plus_dungeon_run_counts'):
                        data[field] = []
                    transport = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: data))
                    result = (ActivityCollector(transport=transport).refresh(self.player, self.season, force=True) if full else
                              refresh_raid_progression(self.player, apply=True, transport=transport))
                    self.assertEqual(result['status'], 'partial')
                    self.row.refresh_from_db()
                    self.assertEqual(self.row.payload_json[META]['status'], 'partial')
                    self.assertLess(_date(self.row.payload_json[META]['next_attempt_at']), timezone.now() + timedelta(hours=2))
                    view = project_player_activity(self.player)['raid_progression']
                    self.assertEqual(view['status'], 'partial')
                    self.assertEqual(view['rows'][-1]['mythic'], 0 if cached else None)
                    self.assertEqual(view['rows'][0]['mythic'], 8 if cached else None)
                    self.assertEqual(self.row.payload_json['raid_progression'][slug]['total_bosses'], 1)
                    # Persist explicit zero without turning an incomplete response ready.
                    data['raid_progression'][slug]['normal_bosses_killed'] = 0
                    self.row.payload_json[META].pop('next_attempt_at', None); self.row.save()
                    result = (ActivityCollector(transport=transport).refresh(self.player, self.season, force=True) if full else
                              refresh_raid_progression(self.player, apply=True, transport=transport))
                    self.assertEqual(result['status'], 'partial')
                    self.assertEqual(project_player_activity(self.player)['raid_progression']['rows'][-1]['normal'], 0)

    def test_explicit_field_includes_single_boss_raids(self):
        field = activity_fields(self.season.rio_season, set(self.data['raid_progression']))
        self.assertNotIn('current-tier', field)
        self.assertEqual(set(field.split(',')[3].split(':')[1:]), set(self.data['raid_progression']))
