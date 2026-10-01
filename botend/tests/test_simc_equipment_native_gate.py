"""Positive native evidence is required before equipment-effect combat runs."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from unittest import TestCase

from simc_equipment_control import equipment_rules, mark_equipment_input, prepare_control_input


class NativeEquipmentEffectGateTests(TestCase):
    def setUp(self):
        self.rules = deepcopy(equipment_rules())
        self.calls = []
        self.effects = {'neck': 'effect={ supported type=equip source=item driver=900001 }'}
        self.native_sets = []
        self.drop_prepared_effect = False

    def execute(self, command):
        self.calls.append(command)
        options = dict(arg.split('=', 1) for arg in command[2:])
        code = Path(command[1]).read_text()
        rows, records, ids = [], [], []
        for line in code.splitlines():
            slot, sep, value = line.partition('=')
            if not sep or slot not in ('neck', 'back', 'wrists', 'feet'):
                continue
            item_id = re.search(r'\bid=(\d+)', value)
            if item_id:
                ids.append(int(item_id[1]))
            rows.extend([line, '# ilevel=321,quality=epic,stats=100haste'])
            effect = self.effects.get(slot, '') if item_id else ''
            if self.drop_prepared_effect and '-original.simc' not in command[1]:
                effect = ''
            records.append(f'0.000 name=x slot={slot} stats={{ +100 Haste }} '
                           f'gems={{ +10 Crit }} enchant={{ test_enchant }} {effect} source=Local '
                           'proc_spells={ proc=OnEquip/900001 }')
        for name, pieces, members in self.native_sets:
            if (sum(item_id in members for item_id in ids) >= pieces
                    and f'set_bonus=name={name},pc={pieces},enable=0' not in code):
                records.append(f'0.000 Initialized set bonus: {{ Test Set, {name}, Arms Warrior, {pieces} piece bonus  }}')
        # An unrelated class set remains unchanged, including in the reference.
        if 'set_bonus=midnight_season_1_4pc=1' in code:
            records.append('0.000 Initialized set bonus: { Class Set, midnight_season_1, Arms Warrior, 4 piece bonus (overridden) }')
        Path(options['save']).write_text('\n'.join(rows))
        Path(options['output']).write_text('\n'.join(records))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def prepare(self, gear, slots, control=False):
        code = 'warrior=x\nspec=arms\nlevel=90\n' + gear + '\n'
        marked = mark_equipment_input(code, slots, control=control, rules=self.rules)
        with tempfile.TemporaryDirectory() as directory:
            return prepare_control_input(marked, 'simc', directory, execute=self.execute)

    def test_no_instantiated_target_effect_rejects_normal_and_control(self):
        for evidence in ('', 'proc_spells={ proc=OnEquip/900001 }',
                         'effect={ unsupported type=unknown source=item driver=900001 }',
                         'effect={ unsupported type=equip source=item driver=0 }',
                         'effect={ attached type=equip source=enchant driver=900001 }'):
            self.effects['neck'] = evidence
            for control in (False, True):
                with self.subTest(evidence=evidence, control=control), self.assertRaisesRegex(
                        ValueError, '目标装备.*未加载.*有效.*拒绝生成收益'):
                    self.prepare('neck=,id=100001,ilevel=321', ['neck'], control)

    def test_background_effect_does_not_validate_inert_target(self):
        self.effects = {'back': 'effect={ background type=equip source=item driver=900001 }'}
        with self.assertRaisesRegex(ValueError, '目标装备.*未加载'):
            self.prepare('neck=,id=100001\nback=,id=100002', ['neck'])

    def test_multiple_effect_blocks_and_attachments_are_preserved(self):
        self.effects['neck'] += ' effect={ attached type=equip source=gem driver=900002 }'
        for control in (False, True):
            with self.subTest(control=control):
                result = self.prepare('neck=,id=100001,ilevel=321,gem_id=42,enchant_id=43', ['neck'], control)
                self.assertIn('gem_id=42', result)
                self.assertIn('enchant_id=43', result)
                self.assertEqual('neck=lmonitor_effect_control' in result, control)
        self.assertTrue(all('save_profile_with_actions=0' in call for call in self.calls))

    def test_normal_rejects_effect_lost_during_background_preparation(self):
        self.drop_prepared_effect = True
        with self.assertRaisesRegex(ValueError, '目标装备.*未加载|目标.*效果.*不一致'):
            self.prepare('neck=,id=100001,ilevel=321\nfeet=,id=100003,embellishment=arcanoweave_lining', ['neck'])

    def test_control_rejects_remaining_native_item_effect(self):
        execute = self.execute
        def leaking(command):
            result = execute(command)
            if 'neck=lmonitor_effect_control' in Path(command[1]).read_text():
                output = Path(dict(arg.split('=', 1) for arg in command[2:])['output'])
                output.write_text(output.read_text().replace('source=Local', self.effects['neck'] + ' source=Local'))
            return result
        self.execute = leaking
        with self.assertRaisesRegex(ValueError, '仍包含自带效果'):
            self.prepare('neck=,id=100001,ilevel=321', ['neck'], True)

    def test_known_native_set_without_item_effect_is_accepted_in_both_groups(self):
        row = next(row for row in self.rules['sets'] if row['name'] == 'arcanoweave_trappings')
        self.effects = {}
        self.native_sets = [(row['name'], row['pieces'], row['items'])]
        for control in (False, True):
            with self.subTest(control=control):
                result = self.prepare('wrists=,id=239660\nback=,id=239661\nset_bonus=midnight_season_1_4pc=1',
                                      ['wrists', 'back'], control)
                self.assertEqual('set_bonus=name=arcanoweave_trappings,pc=2,enable=0' in result, control)
                self.assertIn('set_bonus=midnight_season_1_4pc=1', result)

    def test_rules_membership_without_native_set_initialization_is_not_evidence(self):
        self.effects = {}
        for control in (False, True):
            with self.subTest(control=control), self.assertRaisesRegex(ValueError, '目标装备.*未加载'):
                self.prepare('wrists=,id=239660\nback=,id=239661', ['wrists', 'back'], control)

    def test_unknown_target_set_fails_closed_even_with_loaded_item_effect(self):
        self.native_sets = [('unknown_future_set', 2, [100001, 100002])]
        for control in (False, True):
            with self.subTest(control=control), self.assertRaisesRegex(ValueError, '套装.*缺少已核实规则'):
                self.prepare('neck=,id=100001\nback=,id=100002', ['neck', 'back'], control)

    def test_background_embellishments_are_removed_without_losing_known_target_set(self):
        row = next(row for row in self.rules['sets'] if row['name'] == 'arcanoweave_trappings')
        self.native_sets = [(row['name'], row['pieces'], row['items'])]
        self.effects = {'feet': 'effect={ lining type=equip source=item driver=1283697 }'}
        result = self.prepare('wrists=,id=239660\nback=,id=239661\nfeet=,id=100003,embellishment=arcanoweave_lining',
                              ['wrists', 'back'])
        self.assertIn('feet=lmonitor_effect_control', result)
        self.assertNotIn('set_bonus=name=arcanoweave_trappings', result)

    def test_control_rejects_known_target_set_that_remains_initialized(self):
        self.native_sets = [('arcanoweave_trappings', 2, [239660, 239661, 239662])]
        execute = self.execute
        def leaking(command):
            result = execute(command)
            if 'neck=lmonitor_effect_control' in Path(command[1]).read_text():
                output = Path(dict(arg.split('=', 1) for arg in command[2:])['output'])
                output.write_text(output.read_text() + '\n0.000 Initialized set bonus: '
                                  '{ Test Set, arcanoweave_trappings, Arms Warrior, 2 piece bonus  }')
            return result
        self.execute = leaking
        with self.assertRaisesRegex(ValueError, '套装.*仍'):
            self.prepare('neck=,id=100001\nwrists=,id=239660\nback=,id=239661',
                         ['neck', 'wrists', 'back'], True)

    def test_unparseable_native_set_roster_fails_closed(self):
        execute = self.execute
        def malformed(command):
            result = execute(command)
            output = Path(dict(arg.split('=', 1) for arg in command[2:])['output'])
            output.write_text(output.read_text() + '\n0.000 Initialized set bonus: { changed_format }')
            return result
        self.execute = malformed
        with self.assertRaisesRegex(ValueError, '套装.*无法识别'):
            self.prepare('neck=,id=100001', ['neck'])

    def test_partial_set_does_not_validate_inert_target(self):
        self.effects = {}
        self.native_sets = [('arcanoweave_trappings', 2, [239660, 239661, 239662])]
        with self.assertRaisesRegex(ValueError, '目标装备.*未加载'):
            self.prepare('wrists=,id=239660', ['wrists'])
