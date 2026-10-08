"""Offline real SimC evidence regressions; no /tmp, binary or database needed."""
import copy
import gzip
import json
import unittest
from pathlib import Path

from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
from botend.tests.test_simc_conditional_core import fixture
from simc_equipment_conditional import validate_pair_witness


class ConditionalBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).with_name('fixtures') / 'conditional_run2_evidence.json.gz'
        cls.base = json.loads(gzip.decompress(path.read_bytes()))['sides']
        cls.policy, cls.expectation, cls.auth = fixture()
        cls.event_id = cls.expectation['relations'][0]['consumer_event_spell_ids'][0]

    def event(self, side):
        return next(s for s in side['report']['sim']['players'][0]['stats'] if s['id'] == self.event_id)

    def validate(self, data):
        sides = [validate_equipment_effect_report(data[m]['html'], data[m]['params'],
            native_proof=data[m]['proof'], prepared_input=data[m]['prepared'],
            report_json=data[m]['report'], conditional_authorization=self.auth)
            for m in ('normal', 'control')]
        return sides, validate_pair_witness(*sides)

    def test_real_run2_positive_and_single_side_pending(self):
        sides, pair = self.validate(self.base)
        self.assertTrue(pair['valid'], pair)
        self.assertEqual(pair['damage_ratio'], self.expectation['relations'][0]['factor'])
        for side in sides:
            self.assertEqual(side['status'], 'pair_pending')
            self.assertIsNone(validate_pair_witness(side, None)['valid'])

    def test_report_accepts_seven_digit_git_abbreviation_but_rejects_mismatch(self):
        data = copy.deepcopy(self.base)
        short = self.auth['identity']['revision'][:7]
        for side in data.values():
            side['report']['git_revision'] = short
        sides, pair = self.validate(data)
        self.assertTrue(pair['valid'], (sides, pair))
        for bad in (short[:6], '0' * 7, '', None):
            changed = copy.deepcopy(data)
            changed['normal']['report']['git_revision'] = bad
            with self.subTest(revision=bad):
                sides, pair = self.validate(changed)
                self.assertEqual(sides[0]['status'], 'invalid')
                self.assertFalse(pair['valid'])

    def output_pair(self, extra=''):
        """Deliberate fixture mutation, not new combat evidence."""
        from simc_equipment_conditional import digest, input_digest
        from simc_equipment_control import NATIVE_PROOF_MARKER, native_proof_marker
        data = copy.deepcopy(self.base)
        raw = {m: '\n'.join(l for l in s['prepared'].splitlines()
                           if not l.startswith(NATIVE_PROOF_MARKER)) for m, s in data.items()}
        for mode, side in data.items():
            suffix = '\nhtml=' + mode + '.html\njson2=' + mode + '.json\n'
            if mode == 'control':
                suffix += extra
            proof = side['proof']
            proof['input_hashes'] = {m: input_digest(text + suffix) for m, text in raw.items()}
            proof['pair_hash'] = digest({k: v for k, v in proof.items() if k not in ('mode', 'pair_hash')})
            side['prepared'] = raw[mode] + suffix + native_proof_marker(proof)
        return data

    def test_output_paths_only_are_pair_comparable(self):
        sides, pair = self.validate(self.output_pair())
        self.assertTrue(pair['valid'], pair)
        self.assertNotEqual(sides[0]['conditional_witness']['input_hashes'],
                            sides[1]['conditional_witness']['input_hashes'])
        self.assertEqual(sides[0]['conditional_witness']['pair_binding']['schema_version'], 2)

    def test_output_binding_keeps_unknown_and_simulation_options(self):
        for option in ('seed=9', 'threads=9', 'unknown_option=1',
                       'json2=x.json,full_states=1', 'html='):
            with self.subTest(option=option):
                _, pair = self.validate(self.output_pair(option + '\n'))
                self.assertFalse(pair['valid'], pair)

    def test_output_binding_does_not_replace_full_input_binding(self):
        for line in ('seed=9', 'threads=9', 'waist=,id=1',
                     'actions+=/wait,sec=1', 'html=tampered.html'):
            with self.subTest(line=line):
                data = self.output_pair()
                data['normal']['prepared'] += '\n' + line
                sides, pair = self.validate(data)
                self.assertEqual(sides[0]['status'], 'invalid')
                self.assertIn('prepared input binding', sides[0]['reason'])
                self.assertFalse(pair['valid'])

    def test_report_cannot_supply_pair_authorization(self):
        data = self.output_pair('unknown_option=1\n')
        for side in data.values():
            side['report']['pair_binding'] = {'schema_version': 2, 'input_hashes': {}, 'proof_hash': 'fake'}
            side['report']['conditional_witness'] = {'pair_binding': 'fake'}
        sides, pair = self.validate(data)
        self.assertFalse(pair['valid'])
        for side in sides:
            self.assertNotEqual(side.get('conditional_witness', {}).get('pair_binding'),
                                data['normal']['report']['pair_binding'])

    def test_output_pair_event_and_amplitude_gates_unchanged(self):
        for metric in ('num_executes', 'actual_amount'):
            data = self.output_pair()
            self.event(data['normal'])[metric]['sum'] *= 1.000001
            _, pair = self.validate(data)
            self.assertEqual(pair['reason'], 'event counts mismatch' if metric == 'num_executes'
                             else 'consumer amplitude mismatch')

    def binding_context(self, suffix=''):
        from botend.services.simc_equipment_result_evidence import _native_document
        side = self.base['normal']
        document, complete = _native_document(side['html'])
        self.assertTrue(complete)
        profile = '\n'.join(b for s in document['sections'] if s.get('key') == 'profile'
                            for b in s.get('text_blocks', []))
        name = side['report']['sim']['players'][0]['name']
        context = (side['prepared'], profile, side['report']['sim']['players'][0], side['proof'])
        return json.loads(json.dumps(context).replace(name, name + suffix))

    def test_saved_source_default_is_the_only_implicit_equivalence(self):
        from botend.services.simc_conditional_validation import _bind_report_context
        context = self.binding_context()
        # Mutation tests, not new combat evidence: keep the real actor/APL.
        for index in (0, 1):
            context[index] = '\n'.join(l for l in context[index].splitlines()
                                       if not l.startswith('source='))
        expected = _bind_report_context(*context)
        for frozen_source in (None, 'default'):
            changed = copy.deepcopy(context)
            if frozen_source is not None:
                changed[0] += '\nsource=' + frozen_source
            changed[1] += '\nsource=default'
            result = _bind_report_context(*changed)
            self.assertEqual(result['profile']['source'], 'default')
            self.assertEqual(result['actor'], expected['actor'])
        for frozen_source, saved_source in ((None, 'blizzard'), (None, 'arbitrary'),
                                            ('blizzard', 'default'), ('default', 'blizzard'),
                                            ('blizzard', None), ('', 'default')):
            with self.subTest(frozen=frozen_source, saved=saved_source):
                changed = copy.deepcopy(context)
                if frozen_source is not None:
                    changed[0] += '\nsource=' + frozen_source
                if saved_source is not None:
                    changed[1] += '\nsource=' + saved_source
                with self.assertRaisesRegex(ValueError, 'context mismatch'):
                    _bind_report_context(*changed)

    def test_single_and_both_saved_context_tampering_rejected(self):
        from bs4 import BeautifulSoup
        for modes in (('normal',), ('normal', 'control')):
            for option in ('source=blizzard', 'source=arbitrary', 'unknown_context=1',
                           'race=other', 'actions.precombat=wait,sec=1'):
                with self.subTest(modes=modes, option=option):
                    data = copy.deepcopy(self.base)
                    for mode in modes:
                        soup = BeautifulSoup(data[mode]['html'], 'html.parser')
                        section = next(s for s in soup.select('div.player-section')
                                       if s.find(['h2', 'h3']).get_text(' ', strip=True) == 'Profile')
                        section.find('p').append(BeautifulSoup('<br/>' + option, 'html.parser'))
                        data[mode]['html'] = str(soup)
                    sides, pair = self.validate(data)
                    for mode in modes:
                        self.assertEqual(sides[('normal', 'control').index(mode)]['status'], 'invalid')
                    self.assertFalse(pair['valid'], pair)

    def test_native_apostrophe_actor_keeps_complete_name(self):
        from botend.services.simc_conditional_validation import _bind_report_context
        context = self.binding_context("_San'layn")
        result = _bind_report_context(*context)
        self.assertEqual(result['actor'][1], context[2]['name'])

    def test_native_malformed_secondary_boundaries_fail_closed(self):
        from botend.services.simc_conditional_validation import _bind_report_context
        context = self.binding_context()
        for group in ('original', 'normal', 'control'):
            for marker in ('Initializing items for Player ',
                           'Initializing special effects for Player ',
                           'Creating Auras, Buffs, and Debuffs for Pet '):
                for boundary in ("'Other_San'layn'", "'Other_San'layn'. trailing", "''."):
                    with self.subTest(group=group, marker=marker, boundary=boundary):
                        changed = copy.deepcopy(context)
                        changed[3][group]['effect_log'] += '\n' + marker + boundary
                        with self.assertRaisesRegex(ValueError, 'native actor context mismatch'):
                            _bind_report_context(*changed)

    def test_native_other_actor_and_duplicate_scopes_fail_closed(self):
        from botend.services.simc_conditional_validation import _bind_report_context
        context = self.binding_context()
        for group in ('original', 'normal', 'control'):
            for marker in ('Initializing items for Player ',
                           'Initializing special effects for Player '):
                for name in ("Other_San'layn", context[2]['name']):
                    with self.subTest(group=group, marker=marker, name=name):
                        changed = copy.deepcopy(context)
                        changed[3][group]['effect_log'] += '\n' + marker + "'" + name + "'."
                        with self.assertRaisesRegex(ValueError, 'native actor context mismatch'):
                            _bind_report_context(*changed)

    def test_same_gear_different_json_identity_rejected(self):
        for key, value in (('name', 'Other_Actor'), ('specialization', 'Other Spec'),
                           ('talents', 'other_talents'), ('race', 'other'), ('level', 1), ('role', 'other')):
            with self.subTest(key=key):
                data = copy.deepcopy(self.base)
                data['normal']['report']['sim']['players'][0][key] = value
                sides, pair = self.validate(data)
                self.assertFalse(sides[0]['valid'], sides[0])
                self.assertEqual(sides[0]['status'], 'invalid')
                self.assertFalse(pair['valid'], pair)

    def test_both_reports_other_actor_or_apl_rejected(self):
        for field in ('actor', 'apl'):
            with self.subTest(field=field):
                data = copy.deepcopy(self.base)
                for side in data.values():
                    if field == 'actor':
                        player = side['report']['sim']['players'][0]
                        side['html'] = side['html'].replace(player['name'], 'Other_Actor')
                        player['name'] = 'Other_Actor'
                    else:
                        # Anchor from actual export, not a validator item/spell special case.
                        anchor = next(l for l in side['prepared'].splitlines() if l.startswith('actions.precombat='))
                        self.assertIn(anchor, side['html'])
                        side['html'] = side['html'].replace(anchor, 'actions.precombat=wait,sec=1')
                sides, pair = self.validate(data)
                self.assertEqual([s['status'] for s in sides], ['invalid', 'invalid'])
                self.assertFalse(pair['valid'], pair)

    def test_both_invalid_counts_rejected(self):
        for metric in ('num_executes', 'num_ticks', 'num_tick_results', 'actual_amount'):
            for count in (0.5, True, 0, -1, float('inf'), float('nan'), '99'):
                with self.subTest(metric=metric, count=count):
                    data = copy.deepcopy(self.base)
                    for side in data.values():
                        self.event(side)[metric]['count'] = count
                    sides, pair = self.validate(data)
                    self.assertEqual([s['status'] for s in sides], ['invalid', 'invalid'])
                    self.assertFalse(pair['valid'], pair)

    def test_both_inconsistent_cohort_rejected(self):
        data = copy.deepcopy(self.base)
        for side in data.values():
            self.event(side)['num_executes']['count'] = 1
            self.event(side)['num_tick_results']['count'] = 2
        sides, pair = self.validate(data)
        self.assertEqual([s['status'] for s in sides], ['invalid', 'invalid'])
        self.assertFalse(pair['valid'], pair)

    def test_saved_apl_normalization_preserves_actions(self):
        from bs4 import BeautifulSoup
        from html import escape
        data = copy.deepcopy(self.base)
        for side in data.values():
            soup = BeautifulSoup(side['html'], 'html.parser')
            section = next(s for s in soup.select('div.player-section')
                           if s.find(['h2', 'h3']).get_text(' ', strip=True) == 'Profile')
            paragraph = section.find('p')
            lines = paragraph.get_text('\n').splitlines()
            lists = {}
            for line in lines:
                key, sep, value = line.partition('=')
                if key == 'actions' or key.startswith('actions.') or key == 'actions+':
                    name = key.removesuffix('+').partition('.')[2] or 'default'
                    lists[name] = (lists.get(name, '') if key.endswith('+') else '') + value
            self.assertGreater(len(lists), 1)
            other = [l for l in lines if not l.startswith('actions')]
            # Export splits slash strings and sorts lists; these forms have the
            # same actions. Changes here are deliberate format mutations only.
            normalized = other + ['actions.' + name + '="' + value + '"'
                                  for name, value in reversed(list(lists.items()))]
            paragraph.clear()
            paragraph.append(BeautifulSoup('<br/>'.join(escape(l) for l in normalized), 'html.parser'))
            side['html'] = str(soup)
        sides, pair = self.validate(data)
        self.assertTrue(pair['valid'], pair)

    def test_native_actor_cannot_float_free_of_frozen_input(self):
        from simc_equipment_conditional import digest
        from simc_equipment_control import NATIVE_PROOF_MARKER, native_proof_marker
        data = copy.deepcopy(self.base)
        for side in data.values():
            name = side['report']['sim']['players'][0]['name']
            proof = side['proof']
            for group in ('original', 'normal', 'control'):
                snapshot = proof[group]
                snapshot['effect_log'] = snapshot['effect_log'].replace(name, 'Other_Native_Actor')
                for item in snapshot['items'].values():
                    for effect in item['effects']:
                        if effect.get('origin'):
                            effect['origin']['actor'] = 'Other_Native_Actor'
            proof['pair_hash'] = digest({k: v for k, v in proof.items() if k not in ('mode', 'pair_hash')})
            side['prepared'] = '\n'.join(l for l in side['prepared'].splitlines()
                                         if not l.startswith(NATIVE_PROOF_MARKER)) + '\n' + native_proof_marker(proof)
        sides, pair = self.validate(data)
        self.assertEqual([s['status'] for s in sides], ['invalid', 'invalid'])
        for side in sides:
            self.assertIn('native actor context mismatch', side['reason'])
        self.assertFalse(pair['valid'], pair)

    def test_continuous_sums_and_variable_integral_cohort(self):
        # Not new combat evidence: prove counts are integral, not hard-coded to
        # run2's 99 recorded iterations (options.iterations is 100).
        data = copy.deepcopy(self.base)
        for side in data.values():
            for metric in ('num_executes', 'num_ticks', 'num_tick_results', 'actual_amount'):
                self.event(side)[metric]['count'] = 37.0
            for metric in ('num_executes', 'num_ticks', 'num_tick_results'):
                self.event(side)[metric]['sum'] += 0.25
        sides, pair = self.validate(data)
        self.assertTrue(pair['valid'], pair)


if __name__ == '__main__':
    unittest.main()
