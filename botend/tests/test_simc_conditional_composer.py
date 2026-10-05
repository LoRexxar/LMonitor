"""Central approved facts -> existing candidate/Composer marker, no execution."""
import base64
import json
from copy import deepcopy

from django.test import TestCase

from botend.tests.test_simc_conditional_store import ConditionalStoreTests
from botend.services.simc_conditional_store import freeze_conditional_contract
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.services.simc_composer import SimcComposer
from simc_equipment_control import MARKER, EXPECTATION_MARKER, prepare_control_input


class ConditionalComposerTests(TestCase):
    setUp = ConditionalStoreTests.setUp
    import_source = ConditionalStoreTests.import_source
    approve = ConditionalStoreTests.approve

    def test_reversed_profile_order_preserves_policy_order_through_prepare(self):
        self.approve()
        frozen = freeze_conditional_contract(**self.selector)
        swaps = [{'slot': slot, 'raw_value': 'item,id=' + str(next(
            t['item_id'] for t in frozen['expectation']['targets'] if t['slot'] == slot))}
            for slot in frozen['policy']['target_slots']]
        base = {
            'spec': 'fury', '_trusted_class_name': 'warrior', 'use_ptr': True,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior=Composer\nlevel=90\nrace=human\nspec=fury\n'
                                + '\n'.join(s['slot'] + '=' + s['raw_value']
                                            for s in reversed(swaps)),
            'talent': 'test', 'override_action_list': 'actions=/auto_attack',
            'base_template_content': '{simulation_options}\n{player_identity}\n{talents}\n{equipment}\n{stat_overrides}\n{action_list}\n{output_options}',
            '_result_file_path': 'conditional.html',
        }
        for control in (False, True):
            with self.subTest(control=control):
                params = {'candidate_type': 'gear_swap', 'gear_swaps': deepcopy(swaps),
                          'equipment_effect_policy': deepcopy(frozen['policy']),
                          'equipment_effect_expectation': deepcopy(frozen['expectation'])}
                if control:
                    params['equipment_effect_control'] = True
                before = deepcopy(params)
                code, manifest, error = SimcComposer(None).compose(
                    SimcMonitor.apply_candidate_overrides(deepcopy(base), params))
                self.assertIsNone(error)
                self.assertIsNotNone(manifest)
                self.assertEqual(params, before)
                # A missing binary is the boundary, not a successful SimC run.
                with self.assertRaises(FileNotFoundError):
                    prepare_control_input(code, '/nonexistent/binary', '/nonexistent/directory',
                        conditional_authorization=frozen['conditional_authorization'])
                marker = json.loads(base64.urlsafe_b64decode(next(
                    l[len(MARKER):] for l in code.splitlines() if l.startswith(MARKER))))
                self.assertEqual(list(marker['items']), frozen['policy']['target_slots'])
                self.assertEqual(marker['policy'], frozen['policy'])
                self.assertEqual(marker['control'], control)
                self.assertEqual(marker['items'], {s['slot']: s['raw_value'] for s in swaps})
                with self.assertRaisesRegex(ValueError, 'authorization'):
                    prepare_control_input(code, '/nonexistent/binary', '/nonexistent/directory')
                # Producer normalization must not relax the consumer's strict order.
                marker['items'] = dict(reversed(list(marker['items'].items())))
                forged = '\n'.join(MARKER + base64.urlsafe_b64encode(
                    json.dumps(marker).encode()).decode() if line.startswith(MARKER) else line
                    for line in code.splitlines())
                with self.assertRaisesRegex(ValueError, 'conditional marker mismatch'):
                    prepare_control_input(forged, '/nonexistent/binary', '/nonexistent/directory',
                        conditional_authorization=frozen['conditional_authorization'])

    def test_central_freeze_survives_real_candidate_composer_without_self_authorizing(self):
        self.approve()
        frozen = freeze_conditional_contract(**self.selector)
        swaps = [{'slot': t['slot'], 'raw_value': 'item,id=' + str(t['item_id'])}
                 for t in frozen['expectation']['targets']]
        # Follow the policy's declared slot order, not arbitrary metadata order.
        swaps.sort(key=lambda s: frozen['policy']['target_slots'].index(s['slot']))
        base = {
            'spec': 'fury', '_trusted_class_name': 'warrior', 'use_ptr': True,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior=Composer\nlevel=90\nrace=human\nspec=fury\n'
                                + '\n'.join(s['slot'] + '=' + s['raw_value'] for s in swaps),
            'talent': 'test', 'override_action_list': 'actions=/auto_attack',
            'base_template_content': '{simulation_options}\n{player_identity}\n{talents}\n{equipment}\n{stat_overrides}\n{action_list}\n{output_options}',
            '_result_file_path': 'conditional.html',
        }
        for control in (False, True):
            params = {'candidate_type': 'gear_swap', 'gear_swaps': swaps,
                      'equipment_effect_policy': frozen['policy'],
                      'equipment_effect_expectation': frozen['expectation']}
            if control:
                params['equipment_effect_control'] = True
            request = SimcMonitor.apply_candidate_overrides(deepcopy(base), params)
            code, manifest, error = SimcComposer(None).compose(request)
            self.assertIsNone(error)
            self.assertIsNotNone(manifest)
            marker = json.loads(base64.urlsafe_b64decode(next(
                l[len(MARKER):] for l in code.splitlines() if l.startswith(MARKER))))
            expectation = json.loads(base64.urlsafe_b64decode(next(
                l[len(EXPECTATION_MARKER):] for l in code.splitlines() if l.startswith(EXPECTATION_MARKER))))
            self.assertEqual(marker['version'], 3)
            self.assertEqual(marker['policy'], frozen['policy'])
            self.assertEqual(marker['control'], control)
            self.assertEqual(expectation, frozen['expectation'])
            self.assertNotIn('conditional_authorization', marker)
            # The marker is NOT an approval: rejection precedes binary/file access.
            with self.assertRaisesRegex(ValueError, 'authorization'):
                prepare_control_input(code, '/nonexistent/binary', '/nonexistent/directory')
        bad = deepcopy(params)
        bad['gear_swaps'] = bad['gear_swaps'][:1]
        with self.assertRaises(ValueError):
            SimcMonitor.apply_candidate_overrides(deepcopy(base), bad)
        bad = deepcopy(params)
        bad['equipment_effect_expectation']['identity']['binary_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            SimcMonitor.apply_candidate_overrides(deepcopy(base), bad)
        # Legacy v2 remains a v2 marker, not an implicit opt-in to conditional.
        legacy = deepcopy(params)
        legacy['equipment_effect_policy'] = {k: frozen['policy'][k] for k in ('target_slots', 'rules')}
        legacy['equipment_effect_policy']['version'] = 2
        legacy.pop('equipment_effect_expectation')
        code, _, error = SimcComposer(None).compose(
            SimcMonitor.apply_candidate_overrides(deepcopy(base), legacy))
        self.assertIsNone(error)
        marker = json.loads(base64.urlsafe_b64decode(next(
            l[len(MARKER):] for l in code.splitlines() if l.startswith(MARKER))))
        self.assertEqual(marker['version'], 2)
        self.assertNotIn('policy', marker)
