"""Pure display tests: no ORM, result rewrites or execution/reuse decisions."""
from copy import deepcopy

from django.test import SimpleTestCase

from botend.services.simc_benchmark_noise_display import apply_noise_display


class NoiseDisplayTests(SimpleTestCase):
    def pair(self, *, effect=False, values=(100050, 100000), errors=(40, 40)):
        rows, evidence = [], {}
        for index, (value, error) in enumerate(zip(values, errors)):
            key = f'gear-{index}'
            row = dict(key=key, type='gear_swap', item_id=123, item_variant_key='123:variant',
                       item_level=600 + index * 3, dps=value, baseline_dps=100000,
                       gain_dps=value - 100000, gain_percent=(value - 100000) / 1000)
            normal = dict(task_id=7, run_id=100 + index,
                          dps_error=error, composition_slot_hashes={
                              'talents': 'a' * 64, 'action_list': 'b' * 64,
                              'simulation_options': 'c' * 64, 'player_identity': 'd' * 64,
                              'additional_simc_input': None, 'stat_overrides': None})
            control = None
            if effect:
                row.update(comparison_mode='equipment_effect',
                           effect_validation={'status': 'valid'},
                           effect_delta_percent=max(0, row['gain_percent'])
                           if row['gain_percent'] >= .1 else 0)
                control = deepcopy(normal)
                control.update(run_id=200 + index)
            rows.append(row)
            evidence[key] = {'normal': normal, 'control': control}
        return rows, evidence

    def assert_untouched(self, rows, evidence):
        before = deepcopy((rows, evidence))
        apply_noise_display(rows, evidence)
        self.assertEqual((rows, evidence), before)

    def test_ordinary_tie_preserves_all_raw_values_evidence_and_order(self):
        rows, evidence = self.pair()
        rows.reverse()
        before, proof = deepcopy(rows), deepcopy(evidence)
        self.assertIs(apply_noise_display(rows, evidence), rows)
        self.assertEqual(evidence, proof)
        for row, raw in zip(rows, before):
            self.assertEqual({key: row[key] for key in raw}, raw)
            self.assertEqual(row['display_dps'], 100025)
            self.assertEqual(row['noise_adjustment'], {
                'kind': 'within_reported_error', 'raw_display_value': raw['dps'],
                'display_value': 100025, 'candidate_keys': ['gear-0', 'gear-1'],
                'label': '差异在报告误差范围内，按并列展示'})

    def test_effect_tie_uses_ratio_endpoint_intersection_and_preserves_raw(self):
        rows, evidence = self.pair(effect=True, values=(102050, 102000))
        before, proof = deepcopy(rows), deepcopy(evidence)
        apply_noise_display(rows, evidence)
        for row, raw in zip(rows, before):
            self.assertAlmostEqual(row['effect_delta_percent'], 2.025)
            self.assertEqual({k: row[k] for k in raw if k != 'effect_delta_percent'},
                             {k: v for k, v in raw.items() if k != 'effect_delta_percent'})
            error = evidence[row['key']]['normal']['dps_error']
            low = ((raw['dps'] - error) / (raw['baseline_dps'] + error) - 1) * 100
            high = ((raw['dps'] + error) / (raw['baseline_dps'] - error) - 1) * 100
            self.assertLessEqual(low, row['effect_delta_percent'])
            self.assertGreaterEqual(high, row['effect_delta_percent'])
            self.assertEqual(row['noise_adjustment']['raw_display_value'], raw['effect_delta_percent'])
        self.assertEqual(evidence, proof)

    def test_mean_is_clamped_to_actual_common_intersection(self):
        rows, evidence = self.pair(errors=(10, 100))
        apply_noise_display(rows, evidence)
        self.assertEqual([row['display_dps'] for row in rows], [100040, 100040])

    def test_strict_threshold_including_exact_point_one(self):
        for effect in (False, True):
            for difference in (100, 101, 1000):
                with self.subTest(effect=effect, difference=difference):
                    rows, evidence = self.pair(effect=effect, values=(102000 + difference, 102000),
                                               errors=(1000, 1000))
                    # Explicit decimal boundary, independent of binary float subtraction.
                    if effect:
                        rows[0]['effect_delta_percent'] = 2 + difference / 1000
                    self.assert_untouched(rows, evidence)

    def test_missing_negative_nonfinite_or_boolean_error_is_not_zero(self):
        for effect in (False, True):
            for error in (None, -1, float('inf'), float('nan'), True):
                for side in (('normal', 'control') if effect else ('normal',)):
                    with self.subTest(effect=effect, error=error, side=side):
                        rows, evidence = self.pair(effect=effect, values=(102050, 102000))
                        evidence['gear-0'][side]['dps_error'] = error
                        self.assert_untouched(rows, evidence)

    def test_disjoint_intervals_are_never_tied(self):
        for effect in (False, True):
            rows, evidence = self.pair(effect=effect, values=(102050, 102000), errors=(1, 1))
            self.assert_untouched(rows, evidence)

    def test_same_actual_task_all_sides_and_composition_required(self):
        for effect in (False, True):
            for side in (('normal', 'control') if effect else ('normal',)):
                for field in ('task_id', 'run_id', 'talents', 'action_list', 'simulation_options',
                              'player_identity', 'additional_simc_input', 'stat_overrides'):
                    with self.subTest(effect=effect, side=side, field=field):
                        rows, evidence = self.pair(effect=effect, values=(102050, 102000))
                        run = evidence['gear-1'][side]
                        if field in ('task_id', 'run_id'):
                            run[field] = 8 if field == 'task_id' else None
                        else:
                            run['composition_slot_hashes'][field] = 'different'
                        self.assert_untouched(rows, evidence)
        rows, evidence = self.pair()
        for item in evidence.values():
            item['normal']['composition_slot_hashes']['talents'] = None
        self.assert_untouched(rows, evidence)

    def test_effect_validation_and_control_are_required(self):
        for status in ('invalid', 'unverified', 'pair_pending', None):
            rows, evidence = self.pair(effect=True, values=(102050, 102000))
            rows[0]['effect_validation'] = {'status': status}
            self.assert_untouched(rows, evidence)
        rows, evidence = self.pair(effect=True, values=(102050, 102000))
        evidence['gear-0']['control'] = None
        self.assert_untouched(rows, evidence)
        rows, evidence = self.pair(effect=True, values=(102050, 102000))
        evidence['gear-0']['control']['dps_error'] = 100000
        self.assert_untouched(rows, evidence)

    def test_baseline_nongear_incomplete_or_ambiguous_identity_not_adjusted(self):
        mutations = ({'key': 'baseline'}, {'type': 'talent'}, {'item_id': None},
                     {'item_variant_key': None}, {'item_level': None}, {'item_level': 603})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows, evidence = self.pair()
                rows[0].update(mutation)
                self.assert_untouched(rows, evidence)
        rows, evidence = self.pair(values=(100060, 100030, 100000), errors=(100, 100, 100))
        rows[1]['item_level'] = rows[0]['item_level']
        self.assert_untouched(rows, evidence)

    def test_explicit_contexts_and_variant_identity_do_not_cross(self):
        for mutation in ({'item_id': 124}, {'item_variant_key': 'another'},
                         {'equipment_group_key': 'another'},
                         {'comparison_kind': 'conditional_increment'},
                         {'context_slots': ['head']}, {'changed_slots': ['hands']}):
            with self.subTest(mutation=mutation):
                rows, evidence = self.pair()
                rows[1].update(mutation)
                self.assert_untouched(rows, evidence)
        rows, evidence = self.pair(effect=True, values=(102050, 102000))
        for row in rows:
            row.update(comparison_kind='conditional_increment', context_slots=['head'], changed_slots=['hands'])
        apply_noise_display(rows, evidence)
        self.assertEqual(rows[0]['effect_delta_percent'], rows[1]['effect_delta_percent'])

    def test_ordinary_positive_coordinate_baseline_is_required(self):
        for baseline in (None, 0, -1, float('inf')):
            rows, evidence = self.pair()
            for row in rows:
                row['baseline_dps'] = baseline
            self.assert_untouched(rows, evidence)
        rows, evidence = self.pair()
        rows[1]['baseline_dps'] = 99999
        self.assert_untouched(rows, evidence)
        rows, evidence = self.pair()
        for row in rows:
            del row['baseline_dps']
        rows.append({'key': 'baseline', 'type': 'baseline', 'dps': 100000})
        apply_noise_display(rows, evidence)
        self.assertEqual(rows[0]['display_dps'], rows[1]['display_dps'])
        self.assertNotIn('noise_adjustment', rows[-1])

    def test_denominator_difference_not_attributed_to_noise(self):
        rows, evidence = self.pair(effect=True, values=(102050, 103100), errors=(500, 500))
        rows[1].update(baseline_dps=101000, gain_dps=2100, gain_percent=2100 / 1010,
                       effect_delta_percent=2100 / 1010)
        rows[0].update(dps=102090, gain_dps=2090, gain_percent=2.09, effect_delta_percent=2.09)
        self.assert_untouched(rows, evidence)

    def test_nontransitive_overlap_does_not_create_full_block(self):
        rows, evidence = self.pair(values=(100080, 100040, 100000), errors=(25, 25, 25))
        apply_noise_display(rows, evidence)
        self.assertEqual(rows[0]['noise_adjustment']['candidate_keys'], ['gear-0', 'gear-1'])
        self.assertEqual(rows[1]['display_dps'], rows[0]['display_dps'])
        self.assertNotIn('noise_adjustment', rows[2])

    def test_small_neighbor_gaps_cannot_chain_to_large_block(self):
        for effect in (False, True):
            rows, evidence = self.pair(effect=effect, values=(102150, 102090, 102030), errors=(200, 200, 200))
            apply_noise_display(rows, evidence)
            self.assertEqual(rows[0]['noise_adjustment']['candidate_keys'], ['gear-0', 'gear-1'])
            self.assertNotIn('noise_adjustment', rows[2])

    def test_full_overlap_small_block_can_tie_all_members(self):
        rows, evidence = self.pair(values=(100080, 100040, 100000), errors=(100, 100, 100))
        apply_noise_display(rows, evidence)
        self.assertEqual([row['display_dps'] for row in rows], [100040] * 3)
        self.assertEqual(rows[0]['noise_adjustment']['candidate_keys'], ['gear-0', 'gear-1', 'gear-2'])

    def test_invalid_middle_candidate_is_an_adjacency_barrier(self):
        rows, evidence = self.pair(values=(100080, 100040, 100000), errors=(100, 100, 100))
        evidence['gear-1']['normal']['dps_error'] = None
        self.assert_untouched(rows, evidence)

    def test_separate_tie_blocks_never_create_a_new_boundary_inversion(self):
        for effect in (False, True):
            with self.subTest(effect=effect):
                rows, evidence = self.pair(
                    effect=effect, values=(102094, 102024, 102063, 101980),
                    errors=(200, 200, 200, 200))
                before = deepcopy((rows, evidence))
                # Each descending pair can tie, but their combined span exceeds
                # 0.1pp. Their means would reverse the originally rising middle.
                self.assertLess(rows[1]['dps'], rows[2]['dps'])
                apply_noise_display(rows, evidence)
                self.assertEqual((rows, evidence), before)

    def test_true_zero_negative_gain_and_noninversions_remain_unchanged(self):
        for effect, values in ((False, (100000, 100050)), (False, (100000, 100000)),
                               (True, (100000, 100000)), (True, (100040, 100020)),
                               (True, (99990, 99980))):
            rows, evidence = self.pair(effect=effect, values=values)
            self.assert_untouched(rows, evidence)
        rows, evidence = self.pair(effect=True, values=(100140, 100090), errors=(100, 100))
        apply_noise_display(rows, evidence)
        self.assertTrue(all(row['effect_delta_percent'] == 0 or row['effect_delta_percent'] >= .1
                            for row in rows))
