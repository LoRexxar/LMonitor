"""Stored product facts are immutable while the response supplies display fixes."""
import copy
import json

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from botend.dashboard.api import SimcSkillDamageSnapshotAPIView
from botend.models import SimcSkillDamageSnapshot, SimcSkillDamageSnapshotActor


class SkillDamageDisplayReadModelTests(TestCase):
    def test_existing_actor_response_projects_globals_and_sources_without_rewriting_facts(self):
        component = {'spell_id': 10, 'effect_index': 1, 'effect_id': 100}
        reviewed = {
            'effect_id': 'reviewed_scope:天赋:10:101', 'source_type': 'reviewed_scope',
            'source_kind': 'talent', 'source_class': 'warrior', 'specializations': ['fury'],
            'source_spell_ids': [10], 'global_components': [component],
            'effect_details': [{'source_spell_id': 10, 'effect_index': 1,
                                'base_value': 20, 'value_kind': 'percent', 'label': '直接伤害'}],
            'projections': [], 'excluded_before_probe': True,
        }
        static = {
            'effect_id': 'dbc_global_talent:101', 'source_type': 'talent',
            'source_spell_ids': [10], 'scope_evidence': 'dbc_global_damage_talent',
            'global_components': [{'spell_id': 10, 'effect_indices': [1]}],
            'projections': [], 'runtime_conditions': [], 'excluded_before_probe': True,
        }
        runtime = {
            'effect_id': 'declared_runtime_state:buff.test[self:10]', 'source_type': 'runtime_state',
            'source_spell_ids': [10], 'scope_evidence': 'declared_global_damage_state',
            'excluded_before_probe': True, 'projections': [],
            'runtime_conditions': [{'token': 'buff.test', 'scope': 'self', 'spell_id': 10, 'stacks': 1}],
        }
        actor = {
            'class': 'warrior', 'specialization': 'fury',
            'reviewed_global_effects': [reviewed, {**reviewed, 'source_kind': 'buff',
                'effect_id': 'reviewed_scope:自身状态:10:None'}],
            'global_skill_effects': [static, runtime],
            'actions': [
                {'token': 'parent', 'spell_id': 21, 'display_name': '来源技能',
                 'product': {'final_normalized_damage': 100}},
                {'token': 'proc', 'spell_id': 22, 'display_name': '派生技能', 'parent_token': 'parent',
                 'product': {'final_normalized_damage': 120}},
            ],
        }
        frozen = copy.deepcopy(actor)
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='a' * 40, game_build='12.1.0.69814', schema_revision=44,
            status='succeeded', generated_spec_count=1, generated_action_count=2,
            payload={'payload_format': 'skill_damage_product_v1', 'storage_format': 'per_spec_actor_rows_v1',
                     'wire_schema_revision': 1, 'total_spec_count': 1},
        )
        shard = SimcSkillDamageSnapshotActor.objects.create(
            snapshot=snapshot, ordinal=0, class_name='warrior', specialization='fury',
            actor_payload=actor, unresolved_payload=[], raw_action_count=2, display_action_count=2,
        )
        request = RequestFactory().get('/api/simc-skill-damage/', {'actor_id': shard.pk})
        request.user = get_user_model().objects.create_user(username='display-viewer')
        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        try:
            result = json.loads(b''.join(response.streaming_content))['data']['snapshot']
        finally:
            response.close()
        self.assertEqual(result['identity']['schema_revision'], 44)
        output = result['actors'][0]
        self.assertEqual(len(output['global_skill_effects']), 1)
        self.assertTrue(output['global_skill_effects'][0]['effect_details'])
        self.assertEqual(output['actions'][1]['source_context']['display_label'], '报告来源：来源技能')
        for original, presented in zip(frozen['actions'], output['actions']):
            self.assertEqual(presented['product'], original['product'])
        shard.refresh_from_db()
        self.assertEqual(shard.actor_payload, frozen)
