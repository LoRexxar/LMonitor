"""Character-entry contracts: local facts only, never identity guesses."""
import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.template.loader import render_to_string
from django.test import TestCase
from django.utils import timezone

from botend.models import PortalMplusRun, RaiderIORunRoster
from botend.services import rio_rankings_snapshot as snapshots
from botend.services.character_navigation import character_link
from botend.services.raiderio_run_roster import project_run_rosters
from botend.services.spec_overview_service import SpecOverviewService
from botend.controller.plugins.portal.PortalMplusRunMonitor import PortalMplusRunMonitor
from botend.tests.test_raiderio_run_roster import details


class CharacterEntryProjectionTests(TestCase):
    def setUp(self):
        self.season = 'season-mn-2'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        override = self.settings(RIO_RANKINGS_SNAPSHOT_ROOT=Path(self.temp.name))
        override.enable()
        self.addCleanup(override.disable)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('no network'))
        network.start()
        self.addCleanup(network.stop)

    def run_row(self, run_id=123, party=None, **kwargs):
        return PortalMplusRun.objects.create(season=self.season, region='world', source='raiderio',
            dungeon_slug='kings-rest', dungeon="Kings' Rest", rank=run_id, level=20,
            run_url=f'https://raider.io/mythic-plus-runs/{run_id}',
            party_json=json.dumps(party or [{'name': 'Member0', 'class': 'Priest', 'spec': 'Holy', 'role': 'healer'}]),
            **kwargs)

    def roster_row(self, run_id=123, **kwargs):
        return RaiderIORunRoster.objects.create(rio_season=self.season, run_id=run_id,
            payload_json=details(run_id), fetched_at=timezone.now(), status='ready', **kwargs)

    def publish_file(self, module, body):
        filename = ('a' if module == 'mplus' else 'b') * 32 + '.json'
        snapshots._write(Path(self.temp.name) / filename, {'schema': snapshots.SCHEMA,
            'coordinate': [module, self.season, 'world'], **body})
        snapshots._write(Path(self.temp.name) / 'index.json', {'schema': snapshots.SCHEMA,
            'current_season': self.season, 'entries': {
                snapshots._key(module, self.season, 'world'): {'file': filename}}})

    def test_overview_and_noscript_share_central_player_entry(self):
        player = {'id': 7, 'rank': 1, 'character_name': 'Tester', 'score': 10}
        with patch.object(SpecOverviewService, '_aggregate', return_value=({'players': [player]}, None)), self.assertNumQueries(0):
            result = SpecOverviewService.players('Mage', 'Fire')
        self.assertEqual(result['players'][0]['detail_url'], character_link(player_id=7))
        html = render_to_string('portal/spec_detail/player_list.html', {'players': [player], 'nav': {}})
        self.assertIn('href="/portal/character/?player_id=7"', html)

    def test_legacy_peak_file_and_party_identity_are_projected_without_queries_or_writes(self):
        self.publish_file('peak', {'items': [{'spec_role': 'dps', 'items': [
            {'name': 'Tester', 'realm_name': 'Test Realm', 'rio_region_slug': 'eu'},
            {'name': 'Unknown', 'realm_name': '', 'rio_region_slug': 'world'}]}]})
        before = {p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()}
        with self.assertNumQueries(0):
            peak = snapshots.read_rankings('peak')['items'][0]['items']
        self.assertEqual(peak[0]['character_url'], character_link(region='eu', realm='Test Realm', name='Tester'))
        self.assertEqual(peak[1]['character_url'], '')
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()})
        party = [{'name': 'Tester', 'profile_url': 'https://raider.io/characters/eu/test/Tester'},
                 {'name': 'Path', 'path': '/characters/us/test/Path'}, {'name': 'Unknown'},
                 {'name': 'Bad', 'profile_url': 'https://evil.example/characters/eu/test/Bad'}]
        run = {'party': party}
        self.publish_file('mplus', {'items': [run], 'runs_by_dungeon': {'test': [run]}})
        with self.assertNumQueries(0):
            result = snapshots.read_rankings('mplus', catalog=True)
            filtered = snapshots.read_rankings('mplus', dungeon='test')
        members = result['items'][0]['party']
        self.assertEqual(members[0]['character_url'], character_link(url=party[0]['profile_url']))
        self.assertEqual(members[1]['character_url'], character_link(url='https://raider.io' + party[1]['path']))
        self.assertEqual([m['character_url'] for m in members[2:]], ['', ''])
        self.assertEqual(result['items'], result['runs_by_dungeon']['test'])
        self.assertEqual(result['items'], filtered['items'])

    def test_build_joins_only_same_run_unique_name_and_canonical_spec_without_mutating_party(self):
        row = self.run_row()
        self.roster_row()
        self.run_row(124)  # Same name, no same-run roster: no cross-run match.
        before = row.party_json
        with self.assertNumQueries(2):
            built = snapshots._build('mplus', self.season, 'world')
        members = {r['rank']: r['party'][0] for r in built['runs_by_dungeon']['kings-rest']}
        self.assertEqual(members[123]['profile_url'], 'https://raider.io/characters/eu/test-realm/Member0')
        self.assertFalse(members[124].get('profile_url'))
        row.refresh_from_db()
        self.assertEqual(row.party_json, before)
        row.party_json = json.dumps([{'name': 'Member0', 'class': 'Priest', 'spec': 'Discipline'}])
        row.save(update_fields=['party_json'])
        self.assertFalse(snapshots._build('mplus', self.season, 'world')['items'][0]['party'][0].get('profile_url'))

    def test_build_rejects_ambiguous_invisible_or_other_season_rosters(self):
        self.run_row()
        roster = self.roster_row()
        original = copy.deepcopy(roster.payload_json)
        ambiguous = copy.deepcopy(original)
        ambiguous['roster'][1]['character']['name'] = 'Member0'
        ambiguous['roster'][1]['character']['realm']['slug'] = 'another'
        ambiguous['roster'][1]['character']['path'] = '/characters/eu/another/Member0'
        for changes in ({'payload_json': ambiguous}, {'roster_visible': False}, {'rio_season': 'season-old'}):
            with self.subTest(changes=list(changes)):
                RaiderIORunRoster.objects.filter(pk=roster.pk).update(
                    **{'payload_json': original, 'roster_visible': True, 'rio_season': self.season, **changes})
                member = snapshots._build('mplus', self.season, 'world')['items'][0]['party'][0]
                self.assertFalse(member.get('profile_url'))

    def test_build_validates_legacy_and_season_run_urls_without_repairing_tokens(self):
        row = self.run_row()
        self.roster_row()
        for url, linked in (
            ('https://raider.io/mythic-plus-runs/123', True),
            (f'https://raider.io/mythic-plus-runs/{self.season}/123-kings-rest', True),
            ('https://raider.io/mythic-plus-runs/season-old/123', False),
            ('https://evil.example/mythic-plus-runs/123', False),
            ('https://raider.io/mythic-plus-runs/0123', False),
            ('https://raider.io/mythic-plus-runs/123?other=1', False),
            (' https://raider.io/mythic-plus-runs/123', False),
            ('https://raider.io/mythic-plus-runs/1\n23', False),
        ):
            with self.subTest(url=url):
                row.run_url = url
                row.save(update_fields=['run_url'])
                result = snapshots._build('mplus', self.season, 'world')['items'][0]
                self.assertEqual(bool(result['party'][0].get('profile_url')), linked)

    def test_collector_preserves_only_source_verified_character_paths(self):
        monitor = PortalMplusRunMonitor(Mock(), Mock())
        source = details()
        source['roster'][1]['character']['path'] = '/characters/eu/test-realm/WrongName'
        source['roster'][2]['character'].pop('path')
        source['roster'][3]['character']['profile_url'] = 'https://raider.io' + source['roster'][3]['character'].pop('path')
        source['roster'][4]['character']['region']['slug'] = 'world'
        monitor._upsert_row({'rank': 1, 'run': source}, source='raiderio', season=self.season, region='world')
        party = json.loads(PortalMplusRun.objects.get().party_json)
        self.assertEqual(party[0]['profile_url'], 'https://raider.io/characters/eu/test-realm/Member0')
        self.assertEqual(party[3]['profile_url'], 'https://raider.io/characters/eu/test-realm/Member3')
        for index in (1, 2, 4):
            self.assertFalse(party[index].get('profile_url'))
            self.assertFalse(party[index].get('path'))

    def test_roster_keeps_profile_source_and_links_icon_and_name_together(self):
        from html.parser import HTMLParser
        class MemberLinks(HTMLParser):
            def __init__(self):
                super().__init__(); self.links = []; self.current = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'a':
                    self.current = {'attrs': attrs, 'icons': 0, 'text': ''}
                    self.links.append(self.current)
                elif tag == 'img' and self.current is not None:
                    self.current['icons'] += 1
            def handle_endtag(self, tag):
                if tag == 'a': self.current = None
            def handle_data(self, value):
                if self.current is not None: self.current['text'] += value
        self.roster_row()
        run = {'url': f'https://raider.io/mythic-plus-runs/{self.season}/123',
               'dungeon': "Kings' Rest", 'mythic_level': 20}
        with self.assertNumQueries(1):
            projected = project_run_rosters([run], self.season)[0]
        member = projected['roster'][0]
        self.assertEqual(member['character_url'], character_link(url=member['profile_url']))
        rendered = render_to_string('portal/spec_detail/_player_raiderio_run.html',
            {'run': {**run, **projected}, 'show_roster': True})
        parser = MemberLinks(); parser.feed(rendered)
        links = [link for link in parser.links if link['attrs']['href'] == member['character_url']]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['icons'], 1)
        self.assertIn(member['name'], links[0]['text'])
        self.assertNotIn('target', links[0]['attrs'])
        self.assertEqual(parser.links[0]['attrs']['href'], run['url'])
        self.assertEqual(parser.links[0]['attrs']['target'], '_blank')
