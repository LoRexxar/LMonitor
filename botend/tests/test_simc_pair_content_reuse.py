"""Pair-local byte-identity reuse must not merge health action identities."""
import copy
import gc
import hashlib
import tempfile
import weakref
from types import SimpleNamespace
from unittest import TestCase, mock

from botend.services import simc_skill_damage as damage
from botend.tests.test_simc_reference_condition_context import actor
from botend.tests.test_simc_variant_metadata_budget import talent


class Graph(dict):
    pass


class PairContentReuseTests(TestCase):
    def fixture(self, root, *, different=False, count=1):
        spool = damage._CanonicalActorSpool(root)
        talents = [talent(10 + i) for i in range(count)]
        aliases = {}
        for t in talents:
            for kind, canonical, state in (('talent', 'selected', 'active'),
                                           ('reference', 'reference', 'inactive')):
                aliases[f'skill_damage_{kind}_{t.pk}_trait_{t.node_id}'] = {
                    'canonical_name': canonical, 'talent_effectiveness': state}
        plan = {'aliases': aliases}
        sources = []
        for health in (100, 34):
            rows = []
            for name, factor, entries in (('selected', 1.5, [10, 30]), ('reference', 1, [30])):
                if different and health == 34 and name == 'reference':
                    factor, entries = 1.2, [40]
                row = actor(factor, scenario=factor * 1.1)
                row.update(name=name, selected_trait_ids=entries,
                           global_damage_policy='exclude_before_probe',
                           global_damage_states=[{'token': 'buff.x', 'spell_id': 700}])
                rows.append(row)
            spool.store_many(health, rows)
            sources.extend(rows)
        variants = damage._SpoolBackedTalentVariants(profile=SimpleNamespace(spec='fixture'),
            talents=talents, actor_plan=plan, actor_spool=spool)
        return spool, variants, sources

    def legacy(self, variants):
        return damage._SpoolBackedTalentVariants(profile=variants.profile, talents=variants.talents,
            actor_plan={'aliases': variants.aliases},
            actor_spool=SimpleNamespace(load=variants.actor_spool.load))

    def test_real_spool_decodes_once_per_content_and_keeps_health_action_shells(self):
        with tempfile.TemporaryDirectory() as root:
            spool, variants, _ = self.fixture(root)
            with mock.patch.object(damage.json, 'load', wraps=damage.json.load) as decode:
                item = next(iter(variants))
            self.assertEqual(decode.call_count, 2, 'four roles contain only two complete JSON bodies')
            for high, low in (('high', 'low'), ('reference_high', 'reference_low')):
                self.assertIsNot(item[high], item[low])
                self.assertIsNot(item[high]['actions'], item[low]['actions'])
                self.assertIsNot(item[high]['actions'][0], item[low]['actions'][0])
                self.assertIs(item[high]['actions'][0]['baseline'], item[low]['actions'][0]['baseline'])
                self.assertIs(item[high]['actions'][0]['scenarios'], item[low]['actions'][0]['scenarios'])
            item['low']['actions'][0]['role_only'] = 'low'
            self.assertNotIn('role_only', item['high']['actions'][0])

    def test_content_id_is_exact_written_utf8_and_source_changes_cannot_change_it(self):
        with tempfile.TemporaryDirectory() as root:
            spool, _, sources = self.fixture(root)
            frozen = copy.deepcopy(sources)
            for key, path in spool._paths.items():
                raw = path.read_bytes()
                self.assertEqual(spool.content_identity(*key), (len(raw), hashlib.sha256(raw).hexdigest()))
            identity = spool.content_identity(100, 'selected')
            sources[0]['actions'][0]['baseline']['direct']['hit'] = -1
            self.assertEqual(spool.content_identity(100, 'selected'), identity)
            self.assertEqual(spool.load(100, 'selected'), frozen[0])
            self.assertIsNone(spool.content_identity(100, 'missing'))
            del spool._paths[(34, 'selected')]
            self.assertIsNone(spool.content_identity(34, 'selected'))

    def test_actual_complete_content_difference_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            spool, variants, _ = self.fixture(root, different=True)
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                item = next(iter(variants))
            self.assertEqual(load.call_args_list, [mock.call(100, 'selected'),
                mock.call(100, 'reference'), mock.call(34, 'reference')])
            self.assertEqual(item['reference_low']['selected_trait_ids'], [40])
            self.assertNotEqual(spool.content_identity(100, 'reference'), spool.content_identity(34, 'reference'))

    def test_old_provider_and_absent_content_ids_use_full_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            spool, variants, _ = self.fixture(root)
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                expected = list(self.legacy(variants))
            self.assertEqual(load.call_count, 4)
            with mock.patch.object(spool, 'content_identity', return_value=None), mock.patch.object(
                    spool, 'load', wraps=spool.load) as load:
                self.assertEqual(list(variants), expected)
            self.assertEqual(load.call_count, 4)

    def test_equal_values_with_different_complete_byte_order_still_decode_separately(self):
        with tempfile.TemporaryDirectory() as root:
            _, variants, sources = self.fixture(root)
            spool = damage._CanonicalActorSpool(root + '/ordered')
            spool.store_many(100, sources[:2])
            for row in sources[2:]:
                row['actions'][0] = dict(reversed(list(row['actions'][0].items())))
            self.assertEqual(sources[:2], sources[2:])
            spool.store_many(34, sources[2:])
            variants.actor_spool = spool
            for name in ('selected', 'reference'):
                self.assertNotEqual(spool.content_identity(100, name), spool.content_identity(34, name))
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                self.assertEqual(list(variants), list(self.legacy(variants)))
            self.assertEqual(load.call_count, 8, 'both providers decode all four distinct JSON bodies')

    def test_low_reference_context_and_complete_output_match_legacy_without_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            spool, variants, sources = self.fixture(root, different=True)
            frozen = copy.deepcopy(sources)
            files = {key: path.read_bytes() for key, path in spool._paths.items()}
            legacy = self.legacy(variants)
            expected = damage.flatten_single_talent_damage_variants({}, {}, legacy)
            rows = list(variants)
            before = copy.deepcopy(rows)
            actual = damage.flatten_single_talent_damage_variants({}, {}, variants)
            self.assertEqual(actual, expected)
            self.assertTrue(actual)
            low = [row for row in actual if '血量低于35%' in row['variant'].get('runtime_condition', '')]
            self.assertTrue(low, 'distinct reference baselines must retain low-health rows')
            self.assertTrue(all(row['variant']['reference_context']['trait_entry_ids'] == [40] for row in low))
            damage.flatten_single_talent_damage_variants({}, {}, rows)
            self.assertEqual(rows, before)
            self.assertEqual(sources, frozen)
            self.assertEqual({key: path.read_bytes() for key, path in spool._paths.items()}, files)
            self.assertEqual(damage.classify_global_skill_effects({}, {}, variants),
                             damage.classify_global_skill_effects({}, {}, legacy))

    def test_filtered_and_state_fallback_loads_share_only_full_graphs(self):
        with tempfile.TemporaryDirectory() as root:
            spool, variants, _ = self.fixture(root)
            for consume in (lambda: list(variants.iter_filtered(lambda _: True)),
                            lambda: list(variants._iter_variants(spool.load_state_metadata))):
                with mock.patch.object(spool, 'content_identity', wraps=spool.content_identity) as identity:
                    rows = consume()
                self.assertEqual(identity.call_count, 4 if 'actions' in rows[0]['high'] else 0)
            with mock.patch.object(spool, 'content_identity', side_effect=AssertionError('metadata shared')):
                for iterator in (variants.iter_metadata, variants.iter_action_metadata, variants.iter_state_metadata):
                    self.assertTrue(list(iterator()))
            with mock.patch.object(spool, 'load_state_metadata', return_value={'global_damage_policy': 'legacy'}), mock.patch.object(
                    spool, 'load', wraps=spool.load) as load:
                self.assertTrue(list(variants.iter_state_metadata()))
            self.assertEqual(load.call_count, 2)
            first = spool.load_action_metadata(100, 'selected')
            first['selected_trait_ids'].append(999)
            self.assertEqual(spool.load_identity(100, 'selected')['selected_trait_ids'], [10, 30])
            state = spool.load_state_metadata(100, 'selected')
            state['global_damage_states'][0]['spell_id'] = -1
            self.assertEqual(spool.load_state_metadata(100, 'selected')['global_damage_states'][0]['spell_id'], 700)

    def test_cache_is_pair_local_and_close_releases_decoded_amounts_immediately(self):
        with tempfile.TemporaryDirectory() as root:
            spool, variants, _ = self.fixture(root, count=2)
            refs = []
            original = spool.load
            def tracked(*args):
                if args == (100, 'selected') and refs:
                    self.assertTrue(all(ref() is None for ref in refs),
                                    'previous pair must be released before the next first load')
                row = original(*args)
                value = Graph(row['actions'][0]['baseline'])
                row['actions'][0]['baseline'] = value
                refs.append(weakref.ref(value))
                return row
            for consume in (lambda: iter(variants), lambda: variants.iter_filtered(lambda _: True)):
                with mock.patch.object(spool, 'load', side_effect=tracked) as load:
                    iterator = consume()
                    item = next(iterator)
                    del item
                    item = next(iterator)
                    gc.collect()
                    self.assertTrue(all(ref() is None for ref in refs[:-2]))
                    self.assertEqual(load.call_count, 4)
                    del item
                    iterator.close()
                    self.assertTrue(all(ref() is None for ref in refs))

    def test_same_health_canonical_alias_reuse_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            spool, variants, _ = self.fixture(root)
            variants.aliases['skill_damage_reference_10_trait_10']['canonical_name'] = 'selected'
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                item = next(iter(variants))
            self.assertEqual(load.call_count, 1)
            self.assertIs(item['high']['actions'], item['reference_high']['actions'])
            self.assertIs(item['low']['actions'], item['reference_low']['actions'])
            self.assertIsNot(item['high']['actions'][0], item['low']['actions'][0])
