from copy import deepcopy

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.simc_benchmark_execution import _embellishment_result_display


class EmbellishmentDisplayIdentityTests(TestCase):
    def test_bonus_identity_preserves_direct_matches_and_frozen_candidates(self):
        season = SeasonMeta.objects.create(
            season_key='display', season_name='展示', mplus_zone_id=1, raid_zone_id=2)
        facts = (
            (273059, 'hunter_s_ritual_stone', '猎人仪式石', 13771),
            (273060, 'hunter_s_ritual_stone', '猎人仪式石', 13771),
            (273062, 'coiled_snake_eye', '盘卷蛇眼', 13769),
            (273063, 'coiled_snake_eye', '盘卷蛇眼', 13769),
            (900001, 'unrelated', '不相关材料', 99999),
            (900002, 'not_an_embellishment', '非美化', 13771),
            (900003, 'wrong_variant', '非美化变体', 13771),
            (900004, 'direct', '直接命中', 13771),
        )
        for item_id, token, name, bonus in facts:
            item = WowItemSnapshot.objects.create(
                item_id=item_id, simc_token=token, name_zh=name,
                catalog_type='gem' if item_id == 900002 else 'embellishment',
                description_zh=f'附加制作材料\n提供下列属性：{name}完整特效。\n用于：配方')
            # Direct token facts must win without contaminating bonus fallback.
            if token == 'direct':
                continue
            WowItemVariantSnapshot.objects.create(
                item=item, season=season, batch_key='display', variant_key='q',
                variant_type='gem' if item_id == 900003 else 'embellishment',
                bonus_ids=[bonus, 8960], effects_json=[{'ignored': 'variant effect'}])
        rules = {'embellishments': {
            'hunters_ritual_stone': {'bonus_id': 13771},
            'coiled_snakeeye': {'bonus_id': 13769},
            'direct': {'bonus_id': 13771},
        }}
        candidates = []
        for token in ('hunters_ritual_stone', 'coiled_snakeeye', 'direct'):
            candidates.append({'candidate_key': token, 'candidate_params': {
                'gear_swap': {'raw_value': f',id=1,embellishment={token}'},
                'equipment_effect_policy': {'rules': rules}}})
        candidates.append({'candidate_key': 'bonus', 'candidate_params': {
            'gear_swap': {'raw_value': ',id=1,bonus_id=13771/8960'}}})
        candidates += [
            {'candidate_key': 'generic', 'candidate_params': {
                'gear_swap': {'raw_value': ',id=1,bonus_id=8960'}}},
            {'candidate_key': 'control', 'candidate_params': {
                **deepcopy(candidates[0]['candidate_params']), 'equipment_effect_control': True}},
            {'candidate_key': 'combo', 'candidate_params': {
                **deepcopy(candidates[0]['candidate_params']), 'gear_swaps': [{'item_id': 1}]}},
        ]
        before = deepcopy(candidates)
        with CaptureQueriesContext(connection) as queries:
            result = _embellishment_result_display(candidates)
        self.assertEqual(result, {
            token: {'label': name, 'tooltip': f'{name}完整特效。'}
            for token, name in (('hunters_ritual_stone', '猎人仪式石'),
                                ('coiled_snakeeye', '盘卷蛇眼'), ('direct', '直接命中'),
                                ('bonus', '猎人仪式石'))})
        self.assertEqual(candidates, before)
        self.assertLessEqual(len(queries), 3)
        for query in queries:
            self.assertNotIn('effects_json', query['sql'])
            self.assertNotIn('stats_json', query['sql'])

        # Different names for the same effect remain ambiguous, never guessed.
        WowItemSnapshot.objects.filter(item_id=273060).update(name_zh='冲突名称')
        result = _embellishment_result_display(candidates)
        self.assertEqual(result['hunters_ritual_stone']['label'], 'hunters_ritual_stone')
