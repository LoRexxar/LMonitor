import io
import json
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from botend.models import PlayerSpecTopPlayer, SeasonMeta, PlayerObservationAnalysis, RaiderIOCharacterActivity


class PlayerObservationCommandTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='observation', season_name='Observation',
            rio_season='season-mn-2', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
            realm='test-realm', character_name='Observer', class_name='Priest', spec_name='Holy', rank=1,
            profile_url='https://raider.io/characters/eu/test-realm/Observer')

    def test_dry_run_is_explicit_single_player_and_zero_writes(self):
        output = io.StringIO()
        call_command('generate_player_observation', player_id=self.player.pk, stdout=output)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['mode'], 'dry-run')
        self.assertEqual(plan['player_id'], self.player.pk)
        self.assertEqual(plan['max_runs'], 18)
        self.assertEqual(plan['max_characters'], 40)
        self.assertEqual(plan['ai_calls'], 0)
        self.assertEqual(PlayerObservationAnalysis.objects.count(), 0)
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 0)

    def test_apply_and_refresh_switches_stop_ai_after_rate_limit(self):
        import tempfile
        from unittest.mock import patch
        from django.test import override_settings
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=root), \
                patch('botend.services.player_observation_sources.refresh_observation_sources') as sources, \
                patch('botend.services.player_observation.generate_observation') as generate:
            call_command('generate_player_observation', player_id=self.player.pk,
                         refresh_sources=True, stdout=io.StringIO())
            sources.assert_not_called()
            generate.assert_not_called()
            generate.return_value = {'status': 'ready'}
            call_command('generate_player_observation', player_id=self.player.pk,
                         apply=True, stdout=io.StringIO())
            sources.assert_not_called()
            generate.assert_called_once()
            generate.reset_mock()
            sources.return_value = {'status': 'rate_limited'}
            with self.assertRaises(CommandError):
                call_command('generate_player_observation', player_id=self.player.pk,
                             apply=True, refresh_sources=True, stdout=io.StringIO())
            generate.assert_not_called()
            generate.return_value = {'status': 'engine_error'}
            with self.assertRaises(CommandError):
                call_command('generate_player_observation', player_id=self.player.pk,
                             apply=True, stdout=io.StringIO())

    def test_non_top20_and_wrong_season_are_rejected_before_collection(self):
        for changes in ({'rank': None}, {'rank': 21}, {'rank': 1, 'season_id': self.season.pk + 1}):
            PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(**changes)
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                call_command('generate_player_observation', player_id=self.player.pk, apply=True)
        self.assertEqual(PlayerObservationAnalysis.objects.count(), 0)
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 0)
