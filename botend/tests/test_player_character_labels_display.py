"""One confirmed character-label collection drives the detail template."""
from copy import deepcopy
from django.test import TestCase
from django.template.loader import render_to_string
from botend.tests import test_player_raid_observation as raid_fixtures
from botend.services.player_observation import project_player_observation
from botend.templatetags.player_observation_tags import confirmed_player_observation


class UnifiedCharacterLabelDisplayTests(TestCase):
    def setUp(self):
        raid_fixtures.RaidObservationProjectionTests.setUp(self)
        raid_fixtures.RaidObservationProjectionTests.save_state(self)

    def test_rule_only_character_uses_the_same_confirmed_judgement_collection(self):
        view = project_player_observation(self.player)
        original = deepcopy(view)
        self.assertEqual(view['status'], 'pending')
        projected = confirmed_player_observation(view)
        self.assertEqual([item['code'] for item in projected['display_judgements']], ['raid_consumer'])
        self.assertTrue(projected['has_supported_judgements'])
        self.assertIsNone(projected.get('raid_judgement'))
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation':view})
        self.assertEqual(html.count('data-judgement="raid_consumer"'), 1)
        for text in ('异常团本经历','首次击杀','不代表已确认付费'):
            self.assertIn(text, html)
        self.assertEqual(view, original)

    def test_different_evidence_sources_render_in_one_list_without_candidates(self):
        view = project_player_observation(self.player)
        view.update(status='ready', source_kind='ai', display_judgements=[
            {'code':'joint_progress','label':'行为标签','value':'共同提高成绩','supported':True,
             'evidence_strength':{'level':'strong','confidence':'高'},'reason':'完整确认依据'},
            {'code':'carrying','label':'未确认候选','value':'不应显示的候选','supported':True,
             'evidence_strength':{'level':'partial','confidence':'中'}}])
        projected = confirmed_player_observation(view)
        self.assertEqual({item['code'] for item in projected['display_judgements']}, {'joint_progress','raid_consumer'})
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation':view})
        self.assertEqual(html.count('data-judgement='), 2)
        self.assertEqual(html.count('player-observation__judgements'), 1)
        self.assertIn('完整确认依据', html)
        self.assertNotIn('不应显示的候选', html)
