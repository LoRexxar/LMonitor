"""Schema-3 provenance: exact real logs plus explicitly mutated negatives."""
import json
import re
from pathlib import Path
from unittest import TestCase
import simc_equipment_control as native


class NativeOriginV3Tests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = json.loads(Path(__file__).with_name('fixtures').joinpath('simc_native_origin_v3.json').read_text())

    def effects(self, sample, log=None, profile=None, version=3):
        return native.native_item_effects(profile or sample['profile_value'],
            sample['log'] if log is None else log, sample['slot'], schema_version=version)

    def test_real_attachment_and_initialization_chain(self):
        for name, driver, trigger, runtime_trigger, source in (
            ('iris_neck_crafted', 1251906, 1252383, 1252389, 'native_attachment'),
            ('sunfire_sash_crafted', 1241503, 1241522, 1241502, 'native_initialization_chain'),
        ):
            sample = self.samples[name]
            with self.subTest(name=name):
                effect, = self.effects(sample)
                self.assertEqual((effect['driver'], effect['trigger']), (trigger, runtime_trigger))
                origin = effect['origin']
                self.assertIsNotNone(origin)
                self.assertEqual((origin['driver'], origin['trigger'], origin['source']), (driver, trigger, source))
                self.assertEqual(origin['slot'], sample['slot'])
                if source == 'native_initialization_chain':
                    self.assertNotIn('index', origin)
                else:
                    self.assertEqual(origin['index'], 0)
                self.assertIsNone(self.effects(sample, version=2)[0]['origin'])
                self.assertEqual(self.effects(sample), self.effects(sample, native.native_effect_log(sample['log'])))

    def test_ambiguous_numeric_roster_and_identity_are_rejected(self):
        for name, sample in self.samples.items():
            log = native.native_effect_log(sample['log'])
            driver, trigger = (1251906, 1252383) if name.startswith('iris') else (1241503, 1241522)
            init = next(l for l in log.splitlines() if 'Initializing item-based special effect ' in l and f'driver={driver} ' in l)
            final = next(l for l in log.splitlines() if f' slot={sample["slot"]} ' in l and ' name=' in l)
            actor = re.search(r"Initializing special effects for Player '([^']+)'", log)[1]
            item_name = re.search(r' name=(\S+)', final)[1]
            other = final.replace(f'slot={sample["slot"]} ', 'slot=other_slot ').replace(f'name={item_name} ', 'name=other_item ')
            variants = {
                'competing_driver': log + '\n' + init.replace(f'driver={driver}', 'driver=99999991'),
                'competing_trigger': log + '\n' + init.replace(f'trigger={trigger}', 'trigger=99999992'),
                'duplicate_final': log + '\n' + other,
                'unchanged_competitor': log + '\n' + other.replace(f'driver={trigger}', f'driver={driver}').replace(re.search(rf'driver={driver} trigger=(\d+)', other.replace(f'driver={trigger}', f'driver={driver}'))[0], f'driver={driver} trigger={trigger}'),
                'same_name': log + '\n' + other.replace('name=other_item ', f'name={item_name} '),
                'extra_actor': log + "\nInitializing items for Player 'other'.\nInitializing special effects for Player 'other'.",
                'pet_effect': log + f"\nCreating Auras, Buffs, and Debuffs for Pet 'pet'.\nPlayer pet item 'x' adding effect {driver} (type=equip, index=0)",
                'competing_declaration': log + f"\nPlayer {actor} item 'other_item' adding effect {driver} (type=equip, index=0)",
                'same_item_declaration': log + f"\nPlayer {actor} item '{item_name}' adding effect 99999991 (type=equip, index=0)",
                'missing_init': log.replace(init, ''),
                'missing_trigger': log.replace(init, init.replace(f' trigger={trigger}', '')),
                'wrong_source': log.replace(init, init.replace('source=item', 'source=gem')),
            }
            if name.startswith('sunfire'):
                variants['duplicate_initialization'] = log + '\n' + init
            for label, changed in variants.items():
                with self.subTest(name=name, mutation=label):
                    self.assertIsNone(self.effects(sample, changed)[0]['origin'])
            with self.subTest(name=name, mutation='profile_id'):
                self.assertIsNone(self.effects(sample, profile=sample['profile_value'].replace('id=', 'wrong_id='))[0]['origin'])
            with self.subTest(name=name, mutation='profile_name'):
                self.assertIsNone(self.effects(sample, profile='wrong_name,' + sample['profile_value'].partition(',')[2])[0]['origin'])

    def test_unknown_schema_is_not_guessed(self):
        for version in (1, 4, True, '3', None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.effects(self.samples['iris_neck_crafted'], version=version)
