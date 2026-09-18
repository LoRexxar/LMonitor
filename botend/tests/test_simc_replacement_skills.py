"""复验真实导出中的替换技能、原生解锁条件及完整施法伤害。"""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

from django.test import SimpleTestCase

from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService,
    _discard_empty_runtime_amount_components,
    _mark_empty_runtime_amount_components_unresolved,
    flatten_single_talent_damage_variants,
    project_skill_damage_product_payload,
)


@unittest.skipUnless(os.environ.get('SIMC_REPLACEMENT_EXPORT_DIR'), '需要真实 SimC 导出目录')
class NativeReplacementSkillTests(SimpleTestCase):
    def test_fury_replacements_do_not_leak_into_other_warrior_specs(self):
        for spec in ('arms', 'protection'):
            for folder in ('all-base', 'all-talents'):
                actor = self.actor(folder, f'warrior_{spec}-100.json')
                self.assertFalse(any(a.get('supported') and a.get('reporting_root_token')
                                     in {'bloodbath', 'crushing_blow'} for a in actor['actions']))

    def actor(self, folder, name):
        path = Path(os.environ['SIMC_REPLACEMENT_EXPORT_DIR']) / folder / name
        payload = json.loads(path.read_text(encoding='utf-8'))
        marked = _mark_empty_runtime_amount_components_unresolved(payload)
        service = SimcSkillDamageSnapshotService(SimpleNamespace(
            simc_revision=payload['simc_revision'], game_build=payload['game_build'],
        ), backend=SimpleNamespace())
        service._validate_export(payload)
        _discard_empty_runtime_amount_components(marked)
        return payload['actors'][0]

    def test_replacement_unlock_and_cast_totals(self):
        for health in (100, 34):
            base = self.actor('all-base', f'warrior_fury-{health}.json')
            selected = self.actor('single-talents', f'warrior_fury--119139--{health}.json')
            roots = {'bloodbath', 'crushing_blow'}
            self.assertFalse(any(a.get('supported') and a.get('reporting_root_token') in roots
                                 for a in base['actions']))
            talent = {'id': 119139, 'node_id': 119139, 'name': 'Reckless Abandon', 'tree_type': 'spec'}
            rows = flatten_single_talent_damage_variants(base, base, [{
                'talent': talent, 'high': selected, 'low': selected,
                'reference_high': base, 'reference_low': base,
            }])
            product = project_skill_damage_product_payload({'actors': [{'actions': rows}]})
            for token in roots:
                with self.subTest(health=health, token=token):
                    raw = [a for a in selected['actions'] if a.get('supported')
                           and a.get('reporting_root_token') == token]
                    self.assertEqual(len(raw), 2)
                    expected_ids = {335098, 335100} if token == 'crushing_blow' else {335096, 113344}
                    self.assertEqual({a['spell_id'] for a in raw}, expected_ids)
                    displayed = [a for a in product['actors'][0]['actions'] if a['token'] == token
                                 and not a['variant'].get('scenario_tokens')]
                    self.assertEqual(len(displayed), 1)
                    row = displayed[0]
                    self.assertEqual(row['component_count'], 2)
                    self.assertEqual(row['variant']['talent_id'], 119139)
                    self.assertIn(1719, [c['spell_id'] for c in row['variant']['runtime_conditions']])
                    for targets in (1, 2, 5, 10, 20):
                        total = sum(
                            component['target_expected'][str(targets)] * component['damage_equivalent_count']
                            for action in raw for name in ('direct', 'tick')
                            if (component := action['baseline'].get(name))
                        )
                        self.assertGreater(total, 0)
                        self.assertAlmostEqual(row['product']['final_normalized_damage_by_target'][str(targets)], total)

    def test_same_bleed_spell_preserves_distinct_native_casts(self):
        actor = self.actor('all-talents', 'warrior_fury-100.json')
        dots = [a for a in actor['actions'] if a['spell_id'] == 113344 and a.get('supported')]
        self.assertIn('bloodbath', {a['reporting_root_token'] for a in dots})
        self.assertIn('bloodbath_bladestorm_unhinged', {a['reporting_root_token'] for a in dots})
        rows = flatten_single_talent_damage_variants(actor, copy.deepcopy(actor), [])
        surviving = {a['reporting_root_token'] for a in rows if a['spell_id'] == 113344}
        self.assertTrue({'bloodbath', 'bloodbath_bladestorm_unhinged'} <= surviving)
