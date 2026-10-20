"""Real ORM regression: one WCL zone contains three current physical raids."""
import copy
import json
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TestCase, override_settings

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import SeasonMeta
from botend.services.spec_overview_service import SpecOverviewService


@override_settings(ALLOWED_HOSTS=['testserver'])
class RaidInstanceGroupingTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        setting = override_settings(MEDIA_ROOT=self.tmp.name)
        setting.enable()
        self.addCleanup(setting.disable)
        cache.clear()
        self.addCleanup(cache.clear)
        self.groups = [
            (1320, '烈毒之渊', [3470, 3445, 3455, 3497, 3420, 3421, 3429, 3492]),
            (1317, '潮缚石窟', [3379]),
            (1324, '基希克斯破封', [3513]),
        ]
        self.release = JournalRelease.objects.create(build='12.1.0.69587+ptr', status='ready')
        JournalState.objects.create(key='wow-zhCN', active_release=self.release)
        self.instances = []
        for iid, name, ids in self.groups:
            instance = JournalInstance.objects.create(release=self.release, journal_id=iid, name=name, kind='raid')
            self.instances.append(instance)
            for index, bid in enumerate(ids):
                JournalEncounter.objects.create(instance=instance, journal_id=bid, name=str(bid), order=index,
                                                payload={'dungeon_encounter_id': bid})
        self.ids = [bid for _, _, ids in self.groups for bid in ids]
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='Season 2', is_active=True, mplus_zone_id=55, raid_zone_id=53,
                                                raid_encounters=[{'id': bid} for bid in self.ids])
        difficulties = []
        for difficulty in (5, 4):
            bosses = [{'boss_id': bid, 'boss_name': str(bid), 'sample_size': difficulty,
                       'dps': {'avg': bid * difficulty}, 'raid_zone_id': 53,
                       'raid_zone_name': 'The Venomous Abyss', 'raid_zone_cn': '烈毒之渊'} for bid in self.ids]
            difficulties.append({'difficulty': difficulty, 'label': str(difficulty), 'zone_groups': [
                {'zone_id': 53, 'zone_name': 'The Venomous Abyss', 'zone_cn': '烈毒之渊', 'bosses': bosses}]})
        self.payload = {'updated_at': '2026-10-16T06:00:00Z', 'zone_groups': difficulties[0]['zone_groups'],
                        'difficulties': difficulties}
        self.path = Path(self.tmp.name) / 'aggregated' / str(self.season.pk) / 'Mage' / 'Fire' / 'raid.json'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.payload))

    def test_actual_overview_and_detail_show_all_three_without_changing_statistics(self):
        before = self.path.read_bytes()
        response = self.client.get('/portal/api/spec/Mage/Fire/raid/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([z.get('instance_name') for z in data['zone_groups']], [x[1] for x in self.groups])
        self.assertEqual(data['updated_at'], self.payload['updated_at'])
        for option in data['difficulties']:
            self.assertEqual([z['instance_id'] for z in option['zone_groups']], [x[0] for x in self.groups])
            for group in option['zone_groups']:
                self.assertEqual(group['zone_id'], 53)  # WCL and Journal IDs remain different namespaces.
                for boss in group['bosses']:
                    self.assertEqual(boss['dps']['avg'], boss['boss_id'] * option['difficulty'])
                    self.assertEqual(boss['sample_size'], option['difficulty'])
            response = self.client.get('/portal/spec/Mage/Fire/raid/', {'difficulty': option['difficulty']})
            soup = BeautifulSoup(response.content, 'html.parser')
            self.assertEqual([h.get_text(strip=True) for h in soup.select('.zone-header h2')], [x[1] for x in self.groups])
        response = self.client.get('/portal/spec/Mage/Fire/raid/', {'boss_id': 3513, 'difficulty': 4})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '基希克斯破封')
        self.assertEqual(self.path.read_bytes(), before)
        self.season.refresh_from_db()
        self.assertEqual([e['id'] for e in self.season.raid_encounters], self.ids)

    def test_projection_is_immutable_idempotent_and_keeps_unresolved_bosses(self):
        from botend.services.raid_instance_projection import project_raid_instances
        original = copy.deepcopy(self.payload)
        with self.assertNumQueries(1):
            projected = project_raid_instances(self.payload)
        self.assertEqual(self.payload, original)
        self.assertEqual(project_raid_instances(projected), projected)
        # A duplicate current mapping is ambiguous, never guess or discard the statistics.
        JournalEncounter.objects.create(instance=self.instances[0], journal_id=9999, name='ambiguous',
                                        payload={'dungeon_encounter_id': 3513})
        projected = project_raid_instances(self.payload)
        rows = [b for g in projected['zone_groups'] for b in g['bosses']]
        self.assertCountEqual([b['boss_id'] for b in rows], self.ids)
        unresolved = next(g for g in projected['zone_groups'] if any(b['boss_id'] == 3513 for b in g['bosses']))
        self.assertNotIn('instance_id', unresolved)
        JournalState.objects.update(active_release=None)
        self.assertEqual(project_raid_instances(self.payload), self.payload)

    def test_current_central_release_changes_are_not_hidden_by_overview_cache(self):
        first = SpecOverviewService.raid('Mage', 'Fire')
        self.assertEqual(first['zone_groups'][2].get('instance_name'), '基希克斯破封')
        new = JournalRelease.objects.create(build='next', status='ready')
        instance = JournalInstance.objects.create(release=new, journal_id=1324, name='新中央名称', kind='raid')
        JournalEncounter.objects.create(instance=instance, journal_id=2896, name='Boss',
                                        payload={'dungeon_encounter_id': 3513})
        JournalState.objects.update(active_release=new)
        second = SpecOverviewService.raid('Mage', 'Fire')
        self.assertEqual(second['zone_groups'][-1]['instance_name'], '新中央名称')
        self.assertEqual(second['updated_at'], first['updated_at'])
