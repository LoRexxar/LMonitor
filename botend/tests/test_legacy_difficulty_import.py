"""Import real legacy bonus responses with exact-build bulk DB2 evidence."""
import json
from copy import deepcopy
from pathlib import Path
from django.core.exceptions import ValidationError
from django.test import TestCase
from botend.models import SeasonMeta, WowItemSnapshot
from botend.services.wow_item_difficulty_import import import_difficulty_variant


class LegacyBulkImportTests(TestCase):
    def setUp(self):
        p = Path(__file__).parent / 'fixtures'
        self.data = json.loads((p / 'wow_item_legacy_import_69587.json').read_text())
        self.season = SeasonMeta.objects.create(season_key='legacy-bulk-import', season_name='Import', is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='proof-batch')
        for row in self.data:
            WowItemSnapshot.objects.get_or_create(item_id=row['rule']['item_id'], defaults=dict(catalog_type='equipment', slot_key='back', quality=row['rule']['quality'], name='Frozen source item'))

    def call_import(self, row):
        r = row['rule']
        return import_difficulty_variant(rule=r, payload=row['payload'], tooltip_evidence=row['tooltip_evidence'],
            item_id=r['item_id'], data_branch=r['data_branch'], instance_id=r['instance_id'],
            difficulty_id=r['difficulty_id'], item_context=r['item_context'], rule_build=r['build'])

    def test_real_legacy_responses_recompute_before_preview(self):
        for row in self.data:
            with self.subTest(item=row['rule']['item_id'], context=row['rule']['item_context']):
                result = self.call_import(row)
                self.assertEqual(result['status'], 'dry_run')

    def test_real_selector_zero_delta_source_is_valid(self):
        from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts
        from botend.services.wow_item_difficulty_import import _verified_rule
        fixture = json.loads((Path(__file__).parent / 'fixtures/wow_item_selector_public_69587.json').read_text())
        def source(table, field, key):
            return next((e for e in fixture['envelopes'] if e['table'] == table and e['filter'] == ({} if field is None else {field: key})), None)
        rule = resolve_difficulty_bonus_facts(128032, 4, fixture['build'], tables=source)
        self.assertEqual(_verified_rule(rule, 128032, 4, fixture['build'])['item_level'], 16)

    def test_bulk_url_count_and_identity_are_not_optional(self):
        original = self.data[0]
        for mutation in ('url', 'count', 'file_hash', 'unfiltered_identity'):
            row = deepcopy(original)
            d = next(d for d in row['rule']['evidence'] if d['table'] == 'ItemSquishEra')
            if mutation == 'url': d['url'] = d['url'].replace('wago.tools', 'example.com')
            elif mutation == 'count': d['source_total'] -= 1
            elif mutation == 'file_hash': d['file_sha256'] = 'not-a-sha256'
            else: d['key'] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                self.call_import(row)
