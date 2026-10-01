"""Canonical gear metadata must not silently lose executable effect bonuses."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from botend.models import WowItemSnapshot
from botend.services.simc_benchmark_config import _normalize_candidate_params


class EquipmentEffectParameterTests(TestCase):
    def setUp(self):
        WowItemSnapshot.objects.create(item_id=271878, metadata={})

    def params(self, raw=',id=271878,ilevel=321', bonus=None):
        swap={'slot':'legs','raw_value':raw,'item_id':271878}
        if bonus is not None:swap['bonus_id']=bonus
        return {'candidate_type':'gear_swap','is_base':False,'gear_swap':swap}

    def test_declared_canonical_bonus_is_retained_in_executable_input(self):
        result=_normalize_candidate_params('gear_swap',self.params(bonus=[13708,13335]))
        self.assertIn('bonus_id=13708/13335',result['gear_swap']['raw_value'])
        self.assertEqual(_normalize_candidate_params('gear_swap',result),result)

    def test_matching_inline_bonus_is_not_duplicated_or_reordered(self):
        raw=',id=271878,ilevel=321,bonus_id=13335/13708'
        result=_normalize_candidate_params('gear_swap',self.params(raw,bonus=[13708,13335]))
        self.assertEqual(result['gear_swap']['raw_value'],raw)

    def test_conflicting_inline_and_metadata_bonus_is_rejected(self):
        with self.assertRaises(ValidationError):
            _normalize_candidate_params('gear_swap',self.params(',id=271878,ilevel=321,bonus_id=13846',bonus=[13708]))

    def test_invalid_declared_bonus_is_rejected_not_repaired(self):
        for bonus in ([True],[0],[-1],['13708x'],'13708;13335'):
            with self.subTest(bonus=bonus),self.assertRaises(ValidationError):
                _normalize_candidate_params('gear_swap',self.params(bonus=bonus))
