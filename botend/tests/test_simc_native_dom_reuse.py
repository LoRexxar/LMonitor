"""Native evidence must not construct a second DOM for its selected sections."""
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from botend.services.simc_equipment_result_evidence import _native_document, extract_equipment_effect_evidence
from botend.tests.test_simc_equipment_result_evidence import EXPECTED, PARAMS, native_html


class NativeDOMReuseTests(SimpleTestCase):
    def test_one_dom_preserves_child_action_details_and_full_profile(self):
        html = native_html(profile='neck=test,id=987,bonus_id=123/456\nhead=other,id=321')
        html = html.replace('toprow right', 'toprow right childrow')
        constructors = []
        original = BeautifulSoup.__init__

        def counted(soup, markup='', *args, **kwargs):
            constructors.append(markup)
            original(soup, markup, *args, **kwargs)

        # Instrument, rather than replace, the real parser and its dependencies.
        with patch.object(BeautifulSoup, '__init__', counted):
            parsed = _native_document(html)
            result = extract_equipment_effect_evidence(html, PARAMS, EXPECTED, _parsed_native=parsed)
        profile = '\n'.join(block for section in parsed[0]['sections']
                            if section['key'] == 'profile' for block in section['text_blocks'])
        self.assertIn('neck=test,id=987,bonus_id=123/456\nhead=other,id=321', profile)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(result['actions'][0]['actual_amount'], 400)
        self.assertEqual(result['actions'][0]['executes'], 4)
        self.assertEqual(result['actions'][0]['successful_results'], 4)
        self.assertEqual(len(constructors), 1, 'native evidence serialized and reparsed a second DOM')
