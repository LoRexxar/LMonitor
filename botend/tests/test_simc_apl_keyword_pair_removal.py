from django.apps import apps
from django.test import SimpleTestCase


class SimcAplKeywordPairRemovalTests(SimpleTestCase):
    def test_legacy_keyword_model_is_not_registered(self):
        with self.assertRaises(LookupError):
            apps.get_model('botend', 'SimcAplKeywordPair')
