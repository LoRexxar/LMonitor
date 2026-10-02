"""Equipment-effect result evidence: synthetic contracts and captured native HTML.

Set LMONITOR_EFFECT_REPORT_DIR and LMONITOR_EFFECT_PROVENANCE to additionally
exercise all original SHA-verified reports, without creating tasks or DB rows.
"""
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path

from django.test import SimpleTestCase

from botend.services.simc_equipment_result_evidence import extract_equipment_effect_evidence


def native_html(profile='neck=test,id=987,bonus_id=123/456', damage=4, triggers=2,
                uptime=10, damage_spell=9002, buff_spell=9003, executes=4):
    return f'''<html><body><div class="player" id="player1"><h2>Tester: 100 dps</h2>
    <div class="toggle-content"><script type="text/x-deferred-html">
    <table class="sc sort"><thead><tr><th>Damage Stats</th><th>DPS</th><th>DPS%</th>
    <th>Execute</th><th>Count</th></tr></thead><tbody><tr class="toprow right">
    <td><a href="https://www.wowhead.com/spell={damage_spell}">Unrelated Name</a></td>
    <td>{damage}</td><td>1%</td><td>{executes}</td><td>{damage}</td></tr>
    <tr class="details hide"><td><table class="details"><tr><th>Executes</th>
    <th>Direct Results</th><th>Tick Results</th><th>Actual Amount</th></tr>
    <tr><td>{executes}</td><td>{damage}</td><td>0</td><td>{damage * 100}</td></tr></table></td></tr>
    </tbody></table><table class="sc"><thead><tr><th>Dynamic Buffs</th><th>Start</th>
    <th>Refresh</th><th>Total</th><th>Start</th><th>Trigger</th><th>Duration</th>
    <th>Uptime</th><th>Benefit</th><th>Overflow</th><th>Expiry</th></tr></thead><tbody>
    <tr class="right"><td><a href="https://www.wowhead.com/spell={buff_spell}">Different Name</a></td>
    <td>{triggers}</td><td>0</td><td>{triggers}</td><td>1s</td><td>1s</td><td>1s</td>
    <td>{uptime}%</td><td>0%</td><td>0</td><td>0</td></tr></tbody></table>
    <div class="player-section"><h3>Profile</h3><pre>warrior="Tester"\n{profile}</pre></div>
    </script></div></div></body></html>'''


PARAMS = {'candidate_type': 'gear_swap', 'gear_swap': {
    'slot': 'neck', 'item_id': 987, 'raw_value': ',id=987,bonus_id=123/456'}}
EXPECTED = {'driver': [9001], 'damage': [9002], 'buff': [9003]}
# Native identities belong only to this regression fixture, never the helper.
NATIVE_IDS = {
    268265: {'driver': [1317582], 'buff': [1317581]},
    271878: {'driver': [1307906], 'buff': [1307910]},
    281239: {'driver': [1310208], 'damage': [1310209]},
    280799: {'driver': [1309762], 'buff': [1309766], 'damage': [1309782]},
}


class EquipmentEffectEvidenceTests(SimpleTestCase):
    def evidence(self, html=None, params=None, expected=EXPECTED):
        return extract_equipment_effect_evidence(
            native_html() if html is None else html,
            copy.deepcopy(PARAMS if params is None else params), expected)

    def test_native_projection_prunes_ui_but_preserves_consumed_details(self):
        from unittest.mock import patch
        from botend.services import simc_equipment_result_evidence as module
        html = native_html().replace('</table></td></tr>',
            '</table><div>UNUSED_CHART_PAYLOAD</div></td></tr>')
        original = module.BeautifulSoup
        parsed_inputs = []
        def capture(text, *args, **kwargs):
            parsed_inputs.append(text)
            return original(text, *args, **kwargs)
        with patch.object(module, 'BeautifulSoup', side_effect=capture):
            result = self.evidence(html)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(result['actions'][0]['actual_amount'], 400)
        self.assertEqual(result['actions'][0]['executes'], 4)
        self.assertEqual(result['actions'][0]['successful_results'], 4)
        self.assertTrue(parsed_inputs)
        self.assertNotIn('UNUSED_CHART_PAYLOAD', parsed_inputs[0])

    def test_native_projection_equivalence_and_safe_fallback(self):
        from html import escape
        from unittest.mock import patch
        from botend.services import simc_equipment_result_evidence as module
        from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
        from botend.tests.test_simc_equipment_effect_validation import frozen_params
        inline = native_html().replace('<script type="text/x-deferred-html">', '').replace('</script>', '')
        escaped = '<div class="player"><div class="toggle-content">' + escape(inline) + '</div></div>'
        cases = [native_html(), inline, escaped,
                 native_html().replace('>Profile<', '>NotProfile<'),
                 native_html().replace('>Damage Stats<', '>Unknown Stats<'),
                 native_html().replace('toprow right', 'toprow childrow right'),
                 native_html().replace('<th>Executes</th>', '<th>Executes <b>Unknown</b></th>'),
                 native_html().replace('</body>', native_html(damage=0) + '</body>')]
        for html in cases:
            with self.subTest(html=html[:60]):
                with patch.object(module, '_native_evidence_html', side_effect=lambda value: value):
                    expected = validate_equipment_effect_report(html, frozen_params())
                self.assertEqual(validate_equipment_effect_report(html, frozen_params()), expected)
        self.assertEqual(module._native_evidence_html(escaped), escaped)
        with patch.object(module, 'native_html', None):
            self.assertEqual(module._native_evidence_html(inline), inline)
            self.assertEqual(self.evidence(inline)['status'], 'valid')
        with patch.object(module.native_html, 'fromstring', side_effect=module.etree.ParserError('invalid')):
            self.assertEqual(module._native_evidence_html(inline), inline)
            self.assertEqual(self.evidence(inline)['status'], 'valid')

    def test_projection_contracts_and_safe_fallback(self):
        from unittest.mock import patch
        from botend.services import simc_equipment_result_evidence as module
        from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
        from botend.tests.test_simc_equipment_effect_validation import frozen_params
        html = native_html()
        plain = html.replace('<script type="text/x-deferred-html">', '').replace('</script>', '')
        child = html.replace('toprow right', 'toprow right childrow')
        variants = [html, plain, child,
                    html.replace('</body>', native_html(damage=0) + '</body>'),
                    html.replace('>Profile<', '>Unknown<'),
                    html.replace('>Damage Stats<', '>Unknown<'),
                    html.replace('<th>Count</th>', '<th>Unknown</th>'),
                    html.replace('<table class="details">', '<table class="details"></table><table>'),
                    '<div class="player"><broken></div>', 'not html']
        for report in variants:
            for proof in (None, {'rules_hash': '0' * 64}):
                with self.subTest(report=report[:50], proof=proof):
                    with patch.object(module, '_native_evidence_html', side_effect=lambda value: value):
                        expected = validate_equipment_effect_report(report, frozen_params(), native_proof=proof)
                    self.assertEqual(validate_equipment_effect_report(report, frozen_params(), native_proof=proof), expected)
        expected = self.evidence(html)
        malformed = html.replace('<th>Damage Stats</th>', '<th>Damage Stats</th></unexpected>')
        self.assertEqual(module._native_evidence_html(malformed), malformed)
        with patch.object(module, 'native_html', None):
            self.assertEqual(self.evidence(html), expected)
        with patch.object(module.native_html, 'fromstring', side_effect=module.etree.ParserError('broken')):
            self.assertEqual(self.evidence(html), expected)
        from html import escape
        fragment = html.split('<script type="text/x-deferred-html">')[1].split('</script>')[0]
        escaped = '<div class="player"><div class="toggle-content">' + escape(fragment) + '</div></div>'
        self.assertEqual(module._native_evidence_html(escaped), escaped)
        self.assertEqual(self.evidence(escaped), expected)

    def test_dropping_all_details_changes_full_validation(self):
        from bs4 import BeautifulSoup
        from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
        from botend.tests.test_simc_equipment_effect_validation import frozen_params
        # Direct results/amount, not summary count/DPS, determine activation.
        html = native_html().replace('<td>4</td><td>4</td><td>0</td><td>400</td>',
                                     '<td>7</td><td>0</td><td>0</td><td>0</td>')
        good = validate_equipment_effect_report(html, frozen_params())
        soup = BeautifulSoup(html, 'html.parser')
        script = soup.find('script')
        fragment = BeautifulSoup(script.string, 'html.parser')
        for row in fragment.select('tr.details'):
            row.decompose()
        script.string = str(fragment)
        unsafe = validate_equipment_effect_report(str(soup), frozen_params())
        self.assertNotEqual(good, unsafe)
        self.assertEqual(good['actions'][0]['actual_amount'], 0)
        self.assertEqual(good['actions'][0]['executes'], 7)
        self.assertFalse(good['actions'][0]['effective'])
        self.assertTrue(unsafe['actions'][0]['effective'])

    def test_optional_identity_is_unverified_and_summary_is_json_freezable(self):
        params = copy.deepcopy(PARAMS)
        before = copy.deepcopy(params)
        evidence = self.evidence(params=params, expected=None)
        self.assertEqual(evidence['status'], 'unverified')
        self.assertIsNone(evidence['valid'])
        self.assertIn('expected_effect_spell_ids_missing', evidence['reason_codes'])
        self.assertEqual(evidence['targets'][0]['observed']['bonus_ids'], [123, 456])
        self.assertEqual(evidence['actions'][0]['spell_id'], 9002)
        self.assertEqual(json.loads(json.dumps(evidence, allow_nan=False)), evidence)
        self.assertEqual(params, before)

    def test_normal_requires_target_identity_and_each_expected_runtime_category(self):
        evidence = self.evidence()
        self.assertEqual(evidence['status'], 'valid')
        self.assertTrue(evidence['valid'])
        self.assertEqual(evidence['actions'][0]['successful_results'], 4)
        self.assertEqual(evidence['buffs'][0]['trigger_count'], 2)
        self.assertEqual(evidence['buffs'][0]['uptime_pct'], 10)
        self.assertEqual(evidence['observed_spell_ids']['actions'], [9002])
        # A driver commonly has no standalone row; terminal events are evidence.
        self.assertEqual(evidence['expected_spell_ids']['driver'], [9001])
        for html, reason in [
            (native_html(profile='neck=test,id=986,bonus_id=123/456'), 'target_item_mismatch'),
            (native_html(profile='neck=test,id=987,bonus_id=123'), 'target_bonus_mismatch'),
            (native_html(profile='head=test,id=987,bonus_id=123/456'), 'target_slot_missing'),
        ]:
            with self.subTest(reason=reason):
                invalid = self.evidence(html)
                self.assertEqual(invalid['status'], 'invalid')
                self.assertIn(reason, invalid['reason_codes'])

    def test_missing_events_are_unknown_not_proof_of_missing_implementation(self):
        for html, expected, reason in [
            (native_html(damage=0, executes=8), EXPECTED, 'damage_events_missing'),
            (native_html(triggers=0), EXPECTED, 'buff_events_missing'),
            (native_html(uptime=0), EXPECTED, 'buff_events_missing'),
            (native_html(damage_spell=7777), EXPECTED, 'damage_events_missing'),
            (native_html(damage=0, triggers=0, uptime=0),
             {'driver': [9002]}, 'driver_events_missing'),
        ]:
            with self.subTest(reason=reason, expected=expected):
                evidence = self.evidence(html, expected=expected)
                self.assertEqual(evidence['status'], 'unverified')
                self.assertIsNone(evidence['valid'])
                self.assertIn(reason, evidence['reason_codes'])
        # A lack of runtime events does not soften demonstrated Profile errors.
        for profile, reason in [
            ('neck=test,id=986,bonus_id=123/456', 'target_item_mismatch'),
            ('neck=test,id=987,bonus_id=123', 'target_bonus_mismatch'),
            ('head=test,id=987,bonus_id=123/456', 'target_slot_missing'),
        ]:
            with self.subTest(profile=profile):
                evidence = self.evidence(native_html(
                    profile=profile, damage=0, triggers=0, uptime=0))
                self.assertEqual(evidence['status'], 'invalid')
                self.assertIs(evidence['valid'], False)
                self.assertIn(reason, evidence['reason_codes'])

    def test_control_requires_removed_target_without_misattributing_background_events(self):
        control = {**PARAMS, 'equipment_effect_control': True}
        off = native_html(profile='neck=custom,stats=10crit', damage=0, triggers=0, uptime=0)
        self.assertEqual(self.evidence(off, control)['status'], 'valid')
        for html, reason in [
            (native_html(), 'control_target_not_disabled'),

            (native_html(profile='neck=custom,bonus_id=123', damage=0, triggers=0, uptime=0),
             'control_target_not_disabled'),

        ]:
            with self.subTest(reason=reason):
                evidence = self.evidence(html, control)
                self.assertEqual(evidence['status'], 'invalid')
                self.assertIn(reason, evidence['reason_codes'])
        self.assertEqual(self.evidence(off, control, expected=None)['status'], 'unverified')
        shared = self.evidence(native_html(profile='neck=custom,stats=10crit'), control)
        self.assertEqual(shared['status'], 'unverified')
        self.assertIsNone(shared['valid'])
        self.assertIn('control_effect_source_unverified', shared['reason_codes'])

    def test_incomplete_report_cannot_prove_absence_or_activation(self):
        for html in ['<html>spell=9002 100 damage Different Name</html>',
                     native_html().replace('<th>Count</th>', '<th>Unknown Count</th>'),
                     native_html().replace('<th>Total</th>', '<th>Unknown Total</th>')]:
            for params in [PARAMS, {**PARAMS, 'equipment_effect_control': True}]:
                with self.subTest(html=html[:80], control=params.get('equipment_effect_control')):
                    # A demonstrably wrong control target remains invalid even
                    # when event parsing is incomplete; otherwise be conservative.
                    result = self.evidence(html, params)
                    self.assertNotEqual(result['status'], 'valid')
                    self.assertIn('report_evidence_incomplete', result['reason_codes'])
        self.assertEqual(self.evidence('<html></html>')['status'], 'unverified')

    def test_driver_only_and_alternative_ids_do_not_guess_names(self):
        self.assertEqual(self.evidence(expected={'driver': [9002]})['status'], 'valid')
        self.assertEqual(self.evidence(expected={'damage': [8888, 9002]})['status'], 'valid')
        self.assertEqual(self.evidence(expected={'damage': [7777]})['status'], 'unverified')
        self.assertEqual(self.evidence(expected={})['status'], 'unverified')
        self.assertEqual(self.evidence(expected={'damage': ['9002?']})['status'], 'unverified')
        self.assertEqual(self.evidence(expected={'unknown': [9002]})['status'], 'unverified')

    def test_slot_aliases_multiple_targets_and_no_other_actor_contamination(self):
        params = {'candidate_type': 'gear_swap', 'gear_swaps': [
            {'slot': 'shoulder', 'item_id': 987, 'raw_value': ',id=987'},
            {'slot': 'wrist', 'item_id': 986, 'raw_value': ',id=986'}]}
        html = native_html(profile='shoulders=test,id=987\nwrists=test,id=986')
        self.assertEqual(self.evidence(html, params)['status'], 'valid')
        # Only the first player's native deferred fragment is eligible.
        html = native_html(damage=0, triggers=0, uptime=0).replace(
            '</body>', native_html().split('<body>')[1].split('</body>')[0] + '</body>')
        self.assertEqual(self.evidence(html)['status'], 'unverified')
        duplicate = native_html(profile='neck=test,id=987\nneck=other,id=986')
        self.assertIn('target_slot_ambiguous', self.evidence(duplicate)['reason_codes'])

    def test_native_report_matrix(self):
        fixture = Path(__file__).parent / 'fixtures' / 'simc_equipment_effect_evidence.json.gz'
        data = json.loads(gzip.decompress(fixture.read_bytes()))
        report_dir = os.environ.get('LMONITOR_EFFECT_REPORT_DIR')
        if report_dir:
            provenance = json.loads(Path(os.environ['LMONITOR_EFFECT_PROVENANCE']).read_text())
            artifacts = {row['run_id']: row for row in provenance['artifacts']}
            cases = [{'run_id': run['id'], 'params': run['candidate_params'],
                      'sha256': artifacts[run['id']]['content_hash']}
                     for run in provenance['runs']]
        else:
            cases = data['cases']
        results = []
        for case in cases:
            item_id = case['params']['gear_swap']['item_id']
            with self.subTest(run_id=case['run_id'], item_id=item_id):
                if report_dir:
                    raw = (Path(report_dir) / f"run-{case['run_id']}.html").read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), case['sha256'])
                    html = raw.decode('utf-8')
                else:
                    html = case['html']
                    self.assertEqual(hashlib.sha256(html.encode()).hexdigest(), case['fixture_sha256'])
                evidence = self.evidence(html, case['params'], NATIVE_IDS[item_id])
                control = case['params'].get('equipment_effect_control') is True
                expected_status = 'valid' if control or item_id == 280799 else 'unverified'
                self.assertEqual(evidence['status'], expected_status)
                if not control:
                    self.assertEqual(evidence['targets'][0]['observed']['item_id'], item_id)
                    if item_id != 280799:
                        self.assertTrue({'damage_events_missing', 'buff_events_missing'} &
                                        set(evidence['reason_codes']))
                else:
                    self.assertIsNone(evidence['targets'][0]['observed']['item_id'])
                results.append({'run_id': case['run_id'], 'item_id': item_id, **evidence})
        output = os.environ.get('LMONITOR_EFFECT_EVIDENCE_OUTPUT')
        if output:
            Path(output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        self.assertEqual(len(results), 30 if report_dir else 8)
