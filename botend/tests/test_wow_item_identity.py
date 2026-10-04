"""Real 281235 Item/ItemSparse and 18-row catalog regression fixture."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import TestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.simc_equipment_eligibility import EquipmentEligibility
from botend.services.gear_builder import slot_matches, spec_matches, serialize_item
from botend.services.wow_item_display import load_item_tooltip_metadata
from botend.services.simc_benchmark_config import _normalize_candidate_params, _freeze_case_candidates

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/item281235_identity.json').read_text())
OLD, NEW = '12.1.5.69594', '12.1.5.70077'


def fact(build):
    evidence = [deepcopy(row['evidence'][0]) for row in FIXTURE['evidence'] if row['game_build'] == build]
    item, sparse = [page['rows'][0] for page in evidence]
    return {'schema_version': 1, 'item_id': 281235, 'game_build': build,
            'inventory_type': item['InventoryType'], 'slot_key': 'legs' if build == NEW else 'chest',
            'item_class_id': item['ClassID'], 'item_subclass_id': item['SubclassID'],
            'allowable_class_mask': sparse['AllowableClass'], 'name': sparse['Display_lang'],
            'source': {'provider': 'wago_db2', 'evidence': evidence}}


def params(build=NEW, slot='legs'):
    return {'candidate_type': 'gear_swap', 'is_base': False, 'gear_swap': {
        'item_id': 281235, 'slot': slot, 'is_ptr': True, 'game_build': build,
        'source': 'manual', 'raw_value': ',id=281235,ilevel=321'}}


class BuildScopedIdentityTests(TestCase):
    def setUp(self):
        base = deepcopy(FIXTURE['catalog']['items'][0])
        self.item = WowItemSnapshot.objects.create(**base)
        rows = FIXTURE['catalog']['variants']
        self.season = SeasonMeta.objects.create(id=rows[0]['season_id'], season_key='identity',
            season_name='identity', mplus_zone_id=1, raid_zone_id=2, is_active=True,
            gear_batch_key=rows[0]['batch_key'])
        for row in rows:
            WowItemVariantSnapshot.objects.create(**deepcopy(row))
        self.item.metadata['item_identity_by_build'] = {
            build: {'is_ptr': True, 'fact': fact(build)} for build in (OLD, NEW)}
        self.item.metadata['simc_effect_activation_by_build'] = {
            NEW: {'is_ptr': True, 'fact': deepcopy(FIXTURE['activation'])}}
        self.item.save(update_fields=['metadata'])

    def test_journal_exact_build_uses_central_explicit_branch(self):
        from botend.services.journal_tooltip import cached_tooltip
        from botend.services.wow_item_display import item_display_metadata
        result = cached_tooltip('item', 281235, 0, OLD)
        self.assertTrue(result['complete'])
        self.assertEqual(result['name'], "Voidweaver's Vestments")
        self.assertIn(OLD, result['note'])
        display = load_item_tooltip_metadata([{'item_id': 281235, 'game_build': NEW}])[0]
        self.assertEqual(display['item_identity']['is_ptr'], True)
        self.assertEqual(display['stats'], {})
        self.assertEqual(item_display_metadata(281235, self.item, game_build=NEW)['slot_key'], 'legs')
        for context in ({'game_build': NEW, 'is_ptr': False}, {'game_build': '12.1.5.99999'},
                        {'game_build': NEW, 'is_ptr': 1}):
            with self.subTest(context=context), self.assertRaises(ValidationError):
                load_item_tooltip_metadata([{'item_id': 281235, **context}])
        self.item.metadata['item_identity_by_build'][NEW]['is_ptr'] = None
        self.item.save(update_fields=['metadata'])
        with self.assertRaises(ValidationError):
            load_item_tooltip_metadata([{'item_id': 281235, 'game_build': NEW}])

    def test_source_primitives_and_primary_presence_fail_closed(self):
        from botend.services.wow_item_identity import identity_reference
        cases = []
        for key in ('schema_version', 'item_id', 'inventory_type', 'item_class_id',
                    'item_subclass_id', 'allowable_class_mask'):
            for value in (True, 1.0, [], None):
                cases.append(('fact', key, value))
        for table, keys in (('Item', ('ID', 'InventoryType', 'ClassID', 'SubclassID')),
                            ('ItemSparse', ('ID', 'InventoryType', 'AllowableClass',
                                            'OverallQualityID', 'StatModifier_bonusStat_0'))):
            for key in keys:
                for value in (True, 5.0, [], None, '5'):
                    cases.append((table, key, value))
        cases += [('ItemSparse', 'OverallQualityID', -1), ('ItemSparse', 'OverallQualityID', 9),
                  ('ItemSparse', 'StatModifier_bonusStat_0', -2),
                  ('ItemSparse', 'StatModifier_bonusStat_0', 999),
                  ('ItemSparse', 'StatModifier_bonusStat_0', 58),
                  ('ItemSparse', 'Display_lang', []), ('Item', 'ClassID', -1),
                  ('Item', 'SubclassID', -1), ('ItemSparse', 'AllowableClass', -2)]
        for target, key, value in cases:
            bad = fact(NEW)
            row = bad if target == 'fact' else next(
                p['rows'][0] for p in bad['source']['evidence'] if p['table'] == target)
            row[key] = value
            with self.subTest(target=target, key=key, value=value), self.assertRaises(ValidationError):
                identity_reference(bad, is_ptr=True)
        for keys in (['OverallQualityID'], ['StatModifier_bonusStat_0'],
                     [f'StatModifier_bonusStat_{n}' for n in range(10)]):
            bad = fact(NEW)
            sparse = bad['source']['evidence'][1]['rows'][0]
            for key in keys:
                sparse.pop(key)
            with self.subTest(missing=keys), self.assertRaises(ValidationError):
                identity_reference(bad, is_ptr=True)
        # All ten known empty cells are different from missing source facts.
        empty = fact(NEW)
        empty['source']['evidence'][1]['rows'][0].update(
            {f'StatModifier_bonusStat_{n}': -1 for n in range(10)})
        self.assertEqual(identity_reference(empty, is_ptr=True)['primary_stat_options'], [])

    def test_merge_json_hash_roundtrip_and_duplicate_conflicts(self):
        from botend.services.wow_item_identity import merge_item_identity, resolve_item_identity
        before = deepcopy(self.item.metadata)
        for value in (True, 1.0):
            bad = fact(NEW)
            bad['schema_version'] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                merge_item_identity([bad], is_ptr=True)
        self.item.refresh_from_db()
        self.assertEqual(self.item.metadata, before)
        # Even opaque source evidence contributes to the hash: Python's 1 == 1.0
        # must not cause merge to return a hash absent from persistent JSON.
        changed = fact(NEW)
        changed['source']['evidence'][0]['rows'][0]['Material'] = 7.0
        merged = merge_item_identity([changed, deepcopy(changed)], is_ptr=True)
        self.item.refresh_from_db()
        stored = resolve_item_identity(self.item, game_build=NEW, is_ptr=True)
        self.assertEqual(merged['references'], [stored])
        self.assertEqual(merge_item_identity([changed], is_ptr=True)['changed_item_ids'], [])
        conflict = deepcopy(changed)
        conflict['name'] = 'Changed name'
        conflict['source']['evidence'][1]['rows'][0]['Display_lang'] = conflict['name']
        before = deepcopy(self.item.metadata)
        with self.assertRaises(ValidationError):
            merge_item_identity([changed, conflict], is_ptr=True)
        self.item.refresh_from_db()
        self.assertEqual(self.item.metadata, before)

    def test_real_dual_build_eligibility_and_display(self):
        eligibility = EquipmentEligibility([params(), params(OLD, 'chest')])
        self.assertIsNone(eligibility.reason(params(), 'mage_fire'))
        self.assertEqual(eligibility.reason(params(slot='chest'), 'mage_fire')['code'], 'wrong_slot')
        self.assertIsNone(eligibility.reason(params(OLD, 'chest'), 'mage_fire'))
        display = load_item_tooltip_metadata([dict(params()['gear_swap'], item_level=321)])[0]
        self.assertEqual(display['slot_key'], 'legs')
        self.assertEqual(display['name'], "Voidweaver's Leggings")
        self.assertEqual(display['name_zh'], '')
        self.assertEqual(display['stats'], {})
        self.assertFalse(display['tooltip_complete'])
        self.assertEqual(display['game_build'], NEW)

    def test_gear_builder_does_not_or_old_chest(self):
        variant = WowItemVariantSnapshot.objects.select_related('item').first()
        variant.game_build = NEW  # adversarial stale slot projection; not persisted
        variant.compatible_slots = ['legs']
        self.assertFalse(slot_matches(variant, 'chest', 'Mage', 'Fire'))
        self.assertTrue(slot_matches(variant, 'legs', 'Mage', 'Fire'))
        self.assertTrue(spec_matches(variant.item, 'Mage', 'Fire', variant, 'legs'))
        row = serialize_item(variant.item, [variant], 'Mage', 'Fire')
        self.assertEqual(row['slot'], 'legs')
        self.assertEqual(row['name_en'], "Voidweaver's Leggings")

    def test_merge_keeps_all_18_variant_fields_and_base(self):
        from botend.services.wow_item_identity import merge_item_identity
        before = list(WowItemVariantSnapshot.objects.order_by('pk').values())
        base = WowItemSnapshot.objects.values().get(pk=self.item.pk)
        self.assertEqual(len(before), 18)
        self.assertEqual(merge_item_identity([fact(OLD), fact(NEW)], is_ptr=True)['changed_item_ids'], [])
        self.assertEqual(list(WowItemVariantSnapshot.objects.order_by('pk').values()), before)
        self.assertEqual(WowItemSnapshot.objects.values().get(pk=self.item.pk), base)

    def test_exact_missing_and_malformed_source_are_rejected(self):
        from botend.services.wow_item_identity import resolve_item_identity, merge_item_identity
        with self.assertRaises(ValidationError):
            resolve_item_identity(self.item, game_build='12.1.5.99999', is_ptr=True)
        for mutate in (
            lambda f: f['source']['evidence'].pop(),
            lambda f: f['source']['evidence'][0].update(locale='zhCN'),
            lambda f: f['source']['evidence'][0]['filters'].update(build=OLD),
            lambda f: f['source']['evidence'][0]['rows'][0].update(InventoryType=20),
            lambda f: f['source']['evidence'][1]['rows'][0].update(ID=1),
            lambda f: f['source']['evidence'][0].update(url='https://evil.invalid/db2/Item'),
        ):
            bad = fact(NEW)
            mutate(bad)
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                merge_item_identity([bad], is_ptr=True)

    def test_normalize_freeze_pins_identity_activation_and_control(self):
        normalized = _normalize_candidate_params('gear_swap', params())
        self.assertEqual(normalized['gear_swap']['game_build'], NEW)
        candidate = SimpleNamespace(key='legs', label='legs', params=normalized,
            candidate_type='gear_swap', icon_url='', effect='', source_label='')
        frozen = _freeze_case_candidates('mage_fire', [candidate])
        normal, control = frozen[1:]
        swap = normal['candidate_params']['gear_swap']
        self.assertEqual(swap['item_identity']['game_build'], NEW)
        activation = normal['candidate_params']['equipment_effect_expectation']['targets'][0]
        self.assertEqual(activation['game_build'], swap['item_identity']['game_build'])
        self.assertIn('bonus_id=14049', swap['raw_value'])
        self.assertEqual(control['candidate_params']['gear_swap'], swap)
        self.assertNotIn('item_identity', normalized['gear_swap'])

    def test_normal_input_rejects_forged_fields(self):
        for patch in ({'item_id': 1}, {'is_ptr': 'true'}, {'is_ptr': False},
                      {'game_build': '12.1.5.99999'}, {'item_identity': {'slot_key': 'legs'}}):
            bad = params()
            bad['gear_swap'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                _normalize_candidate_params('gear_swap', bad)

    def test_task_boundary_rejects_client_identity_but_accepts_server_freeze(self):
        from botend.services.simc_task_service import _normalize_candidates, TaskCreationError
        from botend.services.wow_item_identity import freeze_equipment_identity
        frozen = freeze_equipment_identity(params(), {self.item.item_id: self.item})
        candidate = {'candidate_key': 'legs', 'candidate_params': frozen}
        with self.assertRaises(TaskCreationError):
            _normalize_candidates([candidate])
        accepted = _normalize_candidates([candidate], trusted_item_identity=True)
        self.assertEqual(accepted[0]['candidate_params'], frozen)
        for patch in ({'game_build': 70077}, {'game_build': ''}, {'is_ptr': 'true'},
                      {'game_build': OLD}, {'item_id': 1}):
            bad = deepcopy(candidate)
            bad['candidate_params']['gear_swap'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(TaskCreationError):
                _normalize_candidates([bad], trusted_item_identity=True)

    def test_numeric_latest_and_branch_are_independent(self):
        from botend.services.wow_item_identity import resolve_item_identity
        # Synthetic version-order adversaries, derived from the real source shape.
        def rebased(build):
            return json.loads(json.dumps(fact(NEW)).replace(NEW, build))
        self.item.metadata['item_identity_by_build'] = {
            '12.1.5.9999': {'is_ptr': True, 'fact': rebased('12.1.5.9999')},
            '12.1.5.10000': {'is_ptr': True, 'fact': rebased('12.1.5.10000')},
            '12.1.5.20000': {'is_ptr': False, 'fact': rebased('12.1.5.20000')},
        }
        self.item.save(update_fields=['metadata'])
        request = params()
        request['gear_swap'].pop('game_build')
        normalized = _normalize_candidate_params('gear_swap', request)
        self.assertEqual(normalized['gear_swap']['game_build'], '12.1.5.10000')
        request['gear_swap']['is_ptr'] = False
        self.assertEqual(_normalize_candidate_params('gear_swap', request)['gear_swap']['game_build'],
                         '12.1.5.20000')
        with self.assertRaises(ValidationError):
            resolve_item_identity(self.item, game_build='12.1.5.20000', is_ptr=True)

    def test_exact_activation_never_falls_back_to_another_build_or_branch(self):
        from botend.services.wow_item_effect_activation_store import freeze_equipment_activation
        facts = self.item.metadata['simc_effect_activation_by_build']
        with self.assertRaises(ValidationError):
            freeze_equipment_activation(params(OLD, 'chest'), {self.item.item_id: facts})
        wrong_branch = deepcopy(facts)
        wrong_branch[NEW]['is_ptr'] = False
        with self.assertRaises(ValidationError):
            freeze_equipment_activation(params(), {self.item.item_id: wrong_branch})
        # Already frozen identity cannot be paired with a different swap build.
        from botend.services.wow_item_identity import freeze_equipment_identity
        frozen = freeze_equipment_identity(params(), {self.item.item_id: self.item})
        frozen['gear_swap']['item_identity']['game_build'] = OLD
        with self.assertRaises(ValidationError):
            freeze_equipment_activation(frozen, {self.item.item_id: facts})

    def test_real_task_creation_freeze_resolve_composer_and_client_rejection(self):
        from botend.models import SimcProfile, SimcApl, SimcContentTemplate, SimcTask
        from botend.tests.test_simc_task_reference_slice import create_test_backend, create_test_talent
        from botend.services.simc_task_service import create_task, initialize_task_runs, TaskCreationError
        from botend.services.task_resolver import resolve_task
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor, _composer_identity
        from botend.services.simc_composer import SimcComposer
        from simc_equipment_control import EXPECTATION_MARKER, MARKER
        import base64
        backend = create_test_backend()
        profile = SimcProfile.objects.create(user_id=771, name='Identity chain', spec='mage_fire',
            class_name='mage', player_config_mode='manual_equipment', is_active=True,
            player_equipment='mage=Identity\nspec=fire\nlevel=90\nlegs=,id=1\nchest=,id=2')
        apl = SimcApl.objects.create(name='Identity APL', spec='mage_fire', content='actions=fireball',
            owner_user_id=771, is_active=True, is_selectable=True)
        template = SimcContentTemplate.objects.create(name='Identity template', spec='mage_fire',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{action_list}\n{output_options}',
            is_active=True, is_selectable=True)
        talent = create_test_talent(user_id=771, spec='mage_fire')
        normalized = _normalize_candidate_params('gear_swap', 'legs=,id=281235,ilevel=321')
        candidate = SimpleNamespace(key='legs', label='legs', params=normalized,
            candidate_type='gear_swap', icon_url='', effect='', source_label='')
        candidates = _freeze_case_candidates('mage_fire', [candidate])
        kwargs = dict(user_id=771, name='Identity task', profile_id=profile.pk, apl_id=apl.pk,
            template_id=template.pk, talent_string_id=talent.pk, backend_id=backend.pk,
            candidates=candidates, mode='comparison', simulation_params={'iterations': 1})
        before_count = SimcTask.objects.count()
        # A real public task creation cannot accept refs, even if JSON claims trust.
        forged = deepcopy(candidates)
        forged[1]['candidate_params']['trusted_item_identity'] = True
        with self.assertRaises(TaskCreationError):
            create_task(**{**kwargs, 'candidates': forged})
        self.assertEqual(SimcTask.objects.count(), before_count)
        task = create_task(**kwargs, is_benchmark_task=True,
                           benchmark_queue_priority=SimcTask.QUEUE_PRIORITY_BENCHMARK_NORMAL)
        task.refresh_from_db()
        frozen = deepcopy(task.mode_params['initial_candidates'])
        self.assertEqual(frozen[1]['candidate_params'], candidates[1]['candidate_params'])
        # Subsequent catalog and live resource changes must not change this task.
        self.item.metadata['item_identity_by_build'] = {}
        self.item.metadata['simc_effect_activation_by_build'] = {}
        self.item.save(update_fields=['metadata'])
        profile.player_equipment = 'mage=Changed\nlegs=,id=999'
        profile.save(update_fields=['player_equipment'])
        runs = initialize_task_runs(task)
        resolved = resolve_task(task)
        spec, class_name = _composer_identity(resolved.resource_metadata['profile']['spec'])
        request = SimcMonitor.apply_candidate_overrides({
            'spec': spec, '_trusted_class_name': class_name,
            'player_import_mode': resolved.profile_payload['player_config_mode'],
            'player_equipment': resolved.profile_content,
            'base_template_content': resolved.template_content,
            'override_action_list': resolved.apl_content,
            'talent': resolved.talent_payload['talent'],
            'iterations': resolved.simulation_params['iterations'],
        }, runs[1].candidate_params)
        code, _, error = SimcComposer(task.user_id).compose(request)
        self.assertIsNone(error)
        markers = {prefix: json.loads(base64.urlsafe_b64decode(next(
            line[len(prefix):] for line in code.splitlines() if line.startswith(prefix))))
            for prefix in (MARKER, EXPECTATION_MARKER)}
        self.assertIn('id=281235,ilevel=321,bonus_id=14049', markers[MARKER]['items']['legs'])
        self.assertEqual(markers[EXPECTATION_MARKER], frozen[1]['candidate_params']['equipment_effect_expectation'])
        self.assertIn('ptr=1', code)
        self.assertNotIn('mage=Changed', code)
        for run, row in zip(runs, frozen):
            run.refresh_from_db()
            self.assertEqual(run.candidate_params, row['candidate_params'])
        task.refresh_from_db()
        self.assertEqual(task.mode_params['initial_candidates'], frozen)

    def test_trusted_frozen_reference_still_rejects_invalid_primitives(self):
        from botend.services.simc_task_service import _normalize_candidates, TaskCreationError
        from botend.services.wow_item_identity import freeze_equipment_identity
        frozen = freeze_equipment_identity(params(), {self.item.item_id: self.item})
        for patch in ({'schema_version': True}, {'schema_version': 1.0}, {'quality': True},
                      {'inventory_type': 7.0}, {'primary_stat_options': None},
                      {'primary_stat_options': ['unknown']}, {'name': []}):
            bad = deepcopy(frozen)
            bad['gear_swap']['item_identity'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(TaskCreationError):
                _normalize_candidates([{'candidate_params': bad}], trusted_item_identity=True)

    def test_frozen_composer_and_historical_runs_do_not_reconsult_catalog(self):
        from botend.models import SimcTask, SimcBackendBinary
        from botend.services.simc_task_service import initialize_task_runs
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        from botend.services.simc_composer import SimcComposer
        from botend.services.simc_benchmark_execution import _identity_display_context
        candidate = SimpleNamespace(key='legs', label='legs', params=params(),
            candidate_type='gear_swap', icon_url='', effect='', source_label='')
        normal = _freeze_case_candidates('mage_fire', [candidate])[1]
        legacy = {'candidate_key': 'legacy', 'candidate_params': params(OLD, 'chest')}
        legacy['candidate_params']['gear_swap'].pop('game_build')
        before = deepcopy([normal, legacy])
        backend = SimcBackendBinary.objects.create(identifier='identity', name='Identity')
        task = SimcTask.objects.create(user_id=771, simc_profile_id=0, name='Identity history', result_file='identity.html',
            backend=backend, mode='comparison', mode_params={'initial_candidates': before})
        # A later catalog change must not enrich legacy input or change a new freeze.
        self.item.metadata['item_identity_by_build'] = {}
        self.item.metadata['simc_effect_activation_by_build'] = {}
        self.item.save(update_fields=['metadata'])
        runs = initialize_task_runs(task)
        self.assertEqual([run.candidate_params for run in runs],
                         [row['candidate_params'] for row in before])
        task.refresh_from_db()
        self.assertEqual(task.mode_params['initial_candidates'], before)
        self.assertEqual(_identity_display_context(legacy['candidate_params']['gear_swap']),
                         {'legacy_identity': True, 'is_ptr': True})
        display = load_item_tooltip_metadata([{
            **legacy['candidate_params']['gear_swap'], **_identity_display_context(
                legacy['candidate_params']['gear_swap'])}])[0]
        self.assertFalse(display['identity_available'])
        request = SimcMonitor.apply_candidate_overrides({
            'spec': 'fire', '_trusted_class_name': 'mage', 'player_import_mode': 'manual_equipment',
            'player_equipment': 'mage=Identity\nspec=fire\nlegs=,id=1\nchest=,id=2',
            'base_template_content': '{simulation_options}\n{player_identity}\n{equipment}\n{action_list}\n{output_options}',
            'override_action_list': 'actions=fireball', 'iterations': 1,
        }, runs[0].candidate_params)
        self.assertEqual(request['_equipment_effect_expectation']['targets'][0]['game_build'], NEW)
        code, _, error = SimcComposer(771).compose(request)
        self.assertIsNone(error)
        import base64
        from simc_equipment_control import MARKER, EXPECTATION_MARKER
        markers = {prefix: json.loads(base64.urlsafe_b64decode(next(
            line[len(prefix):] for line in code.splitlines() if line.startswith(prefix))))
            for prefix in (MARKER, EXPECTATION_MARKER)}
        self.assertIn('id=281235,ilevel=321,bonus_id=14049', markers[MARKER]['items']['legs'])
        self.assertEqual(markers[EXPECTATION_MARKER], normal['candidate_params']['equipment_effect_expectation'])
        self.assertIn('ptr=1', code)
        self.assertEqual(runs[0].candidate_params, normal['candidate_params'])
