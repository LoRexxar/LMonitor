"""Persist real validated side witnesses, then use Benchmark's pair reader."""
import copy
import gzip
import json
from pathlib import Path
from django.test import TestCase
from botend.models import SimcBackendBinary, SimcTask, SimulationRun
from botend.services.simc_benchmark_execution import _equipment_effect_validations, _paired_effect_validation
from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
from botend.tests.test_simc_conditional_core import fixture


class ConditionalPairProjectionTests(TestCase):
    def test_completed_sides_require_real_pair_witness(self):
        evidence=json.loads(gzip.decompress((Path(__file__).with_name('fixtures')/'conditional_run2_evidence.json.gz').read_bytes()))['sides']
        _,expectation,auth=fixture()
        backend=SimcBackendBinary.objects.create(identifier='conditional-pair',name='conditional-pair')
        task=SimcTask.objects.create(user_id=1,name='conditional-pair',simc_profile_id=0,backend=backend)
        sides={}
        for sequence,mode in enumerate(('normal','control'),1):
            data=evidence[mode]
            sides[mode]=validate_equipment_effect_report(data['html'],data['params'],native_proof=data['proof'],prepared_input=data['prepared'],report_json=data['report'],conditional_authorization=auth)
            self.assertEqual(sides[mode]['status'],'pair_pending')
            SimulationRun.objects.create(task=task,sequence=sequence,candidate_key=mode,status='completed',result_summary={'equipment_effect_validation':sides[mode]})
        values=_equipment_effect_validations({(task.pk,'normal'),(task.pk,'control')})
        self.assertEqual(values[(task.pk,'normal')],sides['normal'])
        pair=_paired_effect_validation(values[(task.pk,'normal')],values[(task.pk,'control')])
        self.assertTrue(pair['valid'],pair)
        self.assertEqual(pair['comparison_kind'],'conditional_increment')
        self.assertEqual(pair['damage_ratio'],expectation['relations'][0]['factor'])
        wrong=copy.deepcopy(values[(task.pk,'control')])
        wrong['conditional_witness']['actual_amount']['sum']*=1.5
        self.assertFalse(_paired_effect_validation(sides['normal'],wrong)['valid'])
        self.assertIsNot(_paired_effect_validation(sides['normal'],{'status':'unverified','valid':None})['valid'],True)
        self.assertIsNot(_paired_effect_validation(sides['normal'],{'status':'valid','valid':True})['valid'],True)
        legacy={'status':'valid','valid':True,'schema_version':1}
        self.assertEqual(_paired_effect_validation(legacy,legacy),legacy)
