"""Wago discovery contract; synthetic HTTP boundary, no database writes."""
import html
import json
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.services import wow_data_update as update


class BuildDiscoveryTests(SimpleTestCase):
    def test_latest_product_index_and_branch_isolation(self):
        # Deliberately put a lower patch on a newer build: build wins.
        latest = {
            'wow': {'product': 'wow', 'version': '12.1.5.70575'},
            'wowt': {'product': 'wowt', 'version': '12.1.5.70494'},
            'wowxptr': {'product': 'wowxptr', 'version': '12.1.7.70493'},
            'wow_beta': {'product': 'wow_beta', 'version': '13.0.0.80000'},
            'wow_classic_ptr': {'product': 'wow_classic_ptr', 'version': '5.5.4.90000'},
        }
        with patch.object(update, 'CurrentGearCatalogSource') as factory:
            source = factory.return_value
            # Old unfiltered first page can omit both beta and wowxptr.
            page = {'props': {'builds': {'data': [latest['wow'], latest['wowt']]}}}
            source._get.return_value.text = '<div data-page="' + html.escape(json.dumps(page), quote=True) + '"></div>'
            for branch, product, catalog_branch in (
                ('retail', 'wow', 'live'), ('ptr', 'wowt', 'ptr'),
                ('beta', 'wow_beta', 'beta'),
            ):
                with self.subTest(branch=branch):
                    source.reset_mock()
                    source._get_json.side_effect = [latest, {'wowBuild': 12345}]
                    result = update.UpdateSource().discover(branch)
                    self.assertEqual(result['build'], latest[product]['version'])
                    self.assertEqual(result['product'], product)
                    self.assertEqual(result['catalog_build'], '12345')
                    self.assertEqual(source._get_json.call_args_list[0].args,
                                     ('https://wago.tools/api/builds/latest',))
                    self.assertEqual(source._get_json.call_args_list[1].args,
                                     (f'https://www.raidbots.com/static/data/{catalog_branch}/metadata.json',))
                    source._get.assert_not_called()
            # Either PTR product may be current; absence must not cross branches.
            for products, expected in ((['wowxptr'], 'wowxptr'), (['wowt'], 'wowt'),
                                       (['wow', 'wow_beta', 'wow_classic_ptr'], None)):
                with self.subTest(products=products):
                    source._get_json.side_effect = [{key: latest[key] for key in products}, {}]
                    if expected:
                        self.assertEqual(update.UpdateSource().discover('ptr')['product'], expected)
                    else:
                        with self.assertRaisesRegex(ValueError, 'ptr 没有可用构建'):
                            update.UpdateSource().discover('ptr')
            source._get_json.side_effect = [{'wow': latest['wow']}, {}]
            with self.assertRaisesRegex(ValueError, 'beta 没有可用构建'):
                update.UpdateSource().discover('beta')
            source._get_json.side_effect = [{'wowt': latest['wow_beta']}, {}]
            with self.assertRaises(ValueError):
                update.UpdateSource().discover('ptr')
