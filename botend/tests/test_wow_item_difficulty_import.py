"""Real Wago graph fixture plus explicit synthetic tooltips exercise offline import safety."""
from copy import deepcopy
from unittest.mock import patch
import json
from pathlib import Path
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts
from botend.services.wow_item_difficulty_import import _digest
from django.core.exceptions import ValidationError
from django.test import TestCase
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.wow_item_difficulty_import import import_difficulty_variant


class DifficultyImportTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='import-test', season_name='Test',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='batch')
        self.item = WowItemSnapshot.objects.create(item_id=250243, name='Keep name', quality=4,
            catalog_type='equipment', slot_key='head', metadata={'keep': 1})
        self.legacy = WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
            batch_key='batch', variant_key='legacy', variant_type='drop_equipment', item_level=292,
            stats_json={'stamina': 99})
        self.kw = dict(item_id=250243, data_branch='retail', difficulty_id=1, item_context=1,
                       instance_id=1304, rule_build='12.1.0.69587')
        envelopes = json.loads((Path(__file__).parent / 'fixtures/wow_item_difficulty_item250243_69587.json').read_text())
        for d in envelopes:
            d['provider'] = 'wago'
            d.pop('source_sha256', None)
            d['source_sha256'] = _digest({k: d[k] for k in ('build','table','filter','rows','total')})
        records = {(d['table'], *next(iter(d['filter'].items()))): d for d in envelopes}
        self.kw['rule'] = resolve_difficulty_bonus_facts(250243, 1, '12.1.0.69587',
            tables=lambda t,f,k: records.get((t,f,k)))
        self.kw['rule'].update(data_branch='retail', difficulty_id=1, instance_id=1304)
        self.kw['payload'] = dict(id=250243, quality=3, itemLevel=214,
            tooltip='<!--ilvl-->214<br><!--rf-->+12 Intellect<!--nameDescStats-->')
        self.kw['tooltip_evidence'] = dict(identity_validated=True, item_id=250243,
            data_branch='retail', game_build='', url='https://nether.wowhead.com/tooltip/item/250243?bonus=13442:13437&dataEnv=1')
        self.base_before = list(WowItemSnapshot.objects.values())
        self.variant_before = list(WowItemVariantSnapshot.objects.values())

    def assert_unchanged(self):
        self.assertEqual(list(WowItemSnapshot.objects.values()), self.base_before)
        self.assertEqual(list(WowItemVariantSnapshot.objects.values()), self.variant_before)

    def test_dry_run_and_idempotent_apply_keep_base_and_legacy(self):
        preview = import_difficulty_variant(**self.kw)
        self.assertEqual(preview['status'], 'dry_run')
        self.assert_unchanged()
        first = import_difficulty_variant(**self.kw, apply=True, expected_before_hash=preview['before_hash'])
        self.assertEqual(first['status'], 'created')
        row = WowItemVariantSnapshot.objects.get(pk=first['variant_id'])
        self.assertEqual(row.stats_json, {'intellect': 12})
        self.assertEqual(row.game_build, '')
        self.assertEqual(row.metadata['difficulty_drop']['provider'], 'wago')
        self.assertEqual(row.metadata['tooltip_source']['provider'], 'wowhead')
        before = list(WowItemVariantSnapshot.objects.values())
        self.assertEqual(import_difficulty_variant(**self.kw, apply=True)['status'], 'noop')
        self.assertEqual(list(WowItemVariantSnapshot.objects.values()), before)
        # 公共特效现在归并到中央物品；其他基础资料与身份仍须逐字段保持。
        current = list(WowItemSnapshot.objects.values())
        for field in self.base_before[0]:
            if field not in ('effects', 'updated_at'):
                self.assertEqual(current[0][field], self.base_before[0][field])
        self.assertEqual(current[0]['effects']['schema'], 1)
        from botend.services.wow_item_display import load_item_tooltip_metadata
        self.assertEqual(load_item_tooltip_metadata([{'item_id':250243, 'allow_default_variant':True,
            'default_variant_order':'lowest'}])[0]['variant_id'], self.legacy.pk)

    def test_invalid_artifacts_never_write(self):
        changes = [('rule','status','unresolved'), ('rule','quality',4), ('rule','item_level',215),
            ('rule','build','12.1.0.99999'), ('rule','evidence',[]),
            ('rule','evidence',[{'table':'SyntheticFixture', 'build':'12.1.0.99999'}]),
            ('rule','bonus_ids',[0]),
            ('rule','bonus_ids',[101,999]), ('rule','data_branch','ptr'), ('rule','item_context',2),
            ('payload','id',61), ('payload','quality',True), ('payload','itemLevel',215),
            ('tooltip_evidence','identity_validated',False), ('tooltip_evidence','item_id',61),
            ('tooltip_evidence','game_build','12.1.0.69587'),
            ('tooltip_evidence','url','https://nether.wowhead.com/tooltip/item/61?bonus=101:102'),
            ('tooltip_evidence','url','https://nether.wowhead.com/tooltip/item/250243?bonus=101:102&ilvl=214'),
            ('tooltip_evidence','url','https://nether.wowhead.com/tooltip/item/250243?bonus=999')]
        for container, field, value in changes:
            with self.subTest(container=container, field=field, value=value):
                kw = deepcopy(self.kw); kw[container][field] = value
                with self.assertRaises(ValidationError):
                    import_difficulty_variant(**kw, apply=True)
                self.assert_unchanged()

    def test_missing_base_and_non_equipment_refused(self):
        for value in ('misc', ''):
            WowItemSnapshot.objects.filter(pk=self.item.pk).update(catalog_type=value)
            before = list(WowItemSnapshot.objects.values())
            with self.assertRaises(ValidationError):
                import_difficulty_variant(**self.kw, apply=True)
            self.assertEqual(list(WowItemSnapshot.objects.values()), before)
        self.item.delete()
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**self.kw, apply=True)
        self.assertFalse(WowItemSnapshot.objects.exists())

    def test_sparse_negative_or_zero_attributes_refused(self):
        for text in ('', 'Cloth', '+0 Intellect', '-12 Intellect', '+-12 Intellect'):
            kw = deepcopy(self.kw)
            kw['payload']['tooltip'] = '<!--ilvl-->214<br><!--rf-->' + text + '<!--nameDescStats-->'
            with self.subTest(text=text), self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assert_unchanged()

    def test_effect_only_and_primary_options_only_are_valid(self):
        for text in ('Equip: Grants a shield.', 'Equip: Deals Fire damage.', '+12 [Strength or Intellect]'):
            kw = deepcopy(self.kw)
            kw['payload']['tooltip'] = '<!--ilvl-->214<br><!--rf-->' + text + '<!--nameDescStats-->'
            self.assertEqual(import_difficulty_variant(**kw)['status'], 'dry_run')
            self.assert_unchanged()

    def test_stale_before_hash_refused(self):
        before = import_difficulty_variant(**self.kw)['before_hash']
        WowItemSnapshot.objects.filter(pk=self.item.pk).update(name='Concurrent')
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**self.kw, apply=True, expected_before_hash=before)
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 1)

    def test_active_batch_change_rolls_back_insert(self):
        from botend.services.gear_builder import active_season
        original = active_season
        calls = []
        def changed():
            calls.append(1)
            if len(calls) == 3:
                SeasonMeta.objects.filter(pk=self.season.pk).update(gear_batch_key='changed')
            return original()
        with patch('botend.services.wow_item_difficulty_import.active_season', side_effect=changed):
            with self.assertRaises(ValidationError):
                import_difficulty_variant(**self.kw, apply=True)
        self.season.refresh_from_db()
        self.assertEqual(self.season.gear_batch_key, 'batch')
        self.assert_unchanged()

    def test_same_key_conflicting_payload_is_refused(self):
        import_difficulty_variant(**self.kw, apply=True)
        before = list(WowItemVariantSnapshot.objects.values())
        kw = deepcopy(self.kw)
        kw['payload']['tooltip'] = '<!--ilvl-->214<br>+13 Intellect'
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**kw, apply=True)
        self.assertEqual(list(WowItemVariantSnapshot.objects.values()), before)

    def test_p1_url_raw_numbers_and_evidence_repros(self):
        cases = []
        for suffix in ('bonus=13668:13442:13437',
                       'bonus=13668:13442:13437&dataEnv=2',
                       'bonus=13668:13442:13437&dataEnv=1&dataEnv=1',
                       'bonus=13668:13442:13437:13437&dataEnv=1',
                       'bonus=13668:13442:13437&dataEnv=1&BONUS=999',
                       'bonus=13668:13442:13437&dataEnv=1&iLvL=214'):
            kw = deepcopy(self.kw); kw['tooltip_evidence']['url'] = 'https://nether.wowhead.com/tooltip/item/250243?' + suffix
            cases.append(kw)
        for text in ('+12.5 Intellect', '12.5 Armor', '+12 Intellect<br>-12 [Strength or Intellect]', '+1e2 Intellect<br>+12 Stamina', '+12 Intellect<br><!--dmg-->9 - 16 Damage', '+12 Intellect<br><!--dps-->(4.8 damage per second)'):
            kw = deepcopy(self.kw); kw['payload']['tooltip'] = '<!--ilvl-->214<br><!--rf-->' + text + '<!--nameDescStats-->'
            cases.append(kw)
        for url in ('https://nether.wowhead.com:443/tooltip/item/250243?bonus=13437:13442:13668&dataEnv=1',
                    'https://x@nether.wowhead.com/tooltip/item/250243?bonus=13437:13442:13668&dataEnv=1'):
            kw = deepcopy(self.kw); kw['tooltip_evidence']['url'] = url; cases.append(kw)
        kw = deepcopy(self.kw); kw['rule']['evidence'] = [{'invented': True}]; cases.append(kw)
        kw = deepcopy(self.kw); kw['rule']['evidence'][0]['rows'][0]['ID'] = 999999; cases.append(kw)
        kw = deepcopy(self.kw); kw['rule']['evidence'] = [d for d in kw['rule']['evidence'] if d['table'] != 'CurvePoint']; cases.append(kw)
        for i,kw in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assert_unchanged()

    def test_native_weapon_and_armor_values_preserved(self):
        kw = deepcopy(self.kw)
        kw['payload']['tooltip'] = '<!--ilvl-->214<br><!--rf--><table><tr><td><span><!--dmg-->9 - 16 Damage</span></td><th>Speed <!--spd-->2.60</th></tr></table><!--dps-->(4.8 damage per second)<br>1,234 Armor<br>+12 Intellect<!--nameDescStats-->'
        result = import_difficulty_variant(**kw, apply=True)
        stats = WowItemVariantSnapshot.objects.get(pk=result['variant_id']).stats_json
        self.assertEqual(stats, {'intellect':12, 'armor':1234, 'min_damage':9, 'max_damage':16, 'weapon_dps':4.8, 'weapon_speed':2.6})

    def test_recomputed_graph_rejects_consistent_but_false_claims(self):
        # Align the forged claim, URL and tooltip; only graph replay can reject it.
        kw = deepcopy(self.kw)
        kw['rule']['item_level'] = kw['payload']['itemLevel'] = 215
        kw['payload']['tooltip'] = kw['payload']['tooltip'].replace('214', '215')
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**kw, apply=True)
        self.assert_unchanged()

    def test_weapon_base_requires_complete_weapon_payload(self):
        WowItemSnapshot.objects.filter(pk=self.item.pk).update(item_class_id=2, inventory_type=13)
        before = list(WowItemVariantSnapshot.objects.values())
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**self.kw, apply=True)
        self.assertEqual(list(WowItemVariantSnapshot.objects.values()), before)

    def test_wago_provenance_identity_is_not_a_boolean_assertion(self):
        for field,value in (('provider','invented'), ('url','https://evil.example/api/db2-find/Curve'),
                            ('source_sha256','0'*64), ('build','12.1.0.1'), ('total',999),
                            ('filter',{'ID':999})):
            kw = deepcopy(self.kw)
            d = kw['rule']['evidence'][0]; d[field] = value
            if field != 'source_sha256':
                d['source_sha256'] = _digest({k:d[k] for k in ('build','table','filter','rows','total')})
            with self.subTest(field=field), self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assert_unchanged()

    def test_equal_evidence_duplicates_allowed_conflicts_refused(self):
        kw = deepcopy(self.kw)
        kw['rule']['evidence'].append(deepcopy(kw['rule']['evidence'][0]))
        self.assertEqual(import_difficulty_variant(**kw)['status'], 'dry_run')
        kw['rule']['evidence'][-1]['rows'][0]['ItemBonusTreeID'] += 1
        d = kw['rule']['evidence'][-1]
        d['source_sha256'] = _digest({k:d[k] for k in ('build','table','filter','rows','total')})
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**kw, apply=True)
        self.assert_unchanged()
