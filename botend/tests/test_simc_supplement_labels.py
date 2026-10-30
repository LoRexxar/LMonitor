"""Frozen-current DBC regression; opt in with SIMC_SUPPLEMENT_SOURCE."""
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from scripts import build_simc_specialization_scope as spec
from scripts import build_simc_mastery_scope as mastery
from scripts.simc_scope_resolution import load_dbc

REVISION = '30c4b061f2103132a34952d80c963303b82bf368'
BUILD = '12.1.5.70575'


@unittest.skipUnless(os.environ.get('SIMC_SUPPLEMENT_SOURCE'), 'requires frozen current source')
class SupplementLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(os.environ['SIMC_SUPPLEMENT_SOURCE'])
        cls.spells, cls.effects = load_dbc(cls.source)

    def test_current_collectors_and_display_keep_label_damage_only(self):
        # Explicit test approval only; production identity constants stay unchanged.
        for module in (spec, mastery):
            with self.subTest(module=module.__name__), patch.object(module, 'REVIEWED_REVISION', REVISION), patch.object(module, 'BUILD', BUILD):
                review = module.collect_review(self.source)
                parts = {p['效果ID']: p for r in review['条目'] for p in r['分量']}
                expected = ({1357605: [441172], 1361425: [441224], 1361426: [441224]}
                            if module is spec else {1357546: [431698, 431872]})
                for eid, affected in expected.items():
                    self.assertEqual(parts[eid]['完整DBC集合'], affected)
                    self.assertEqual(parts[eid]['处理'], '保持原样')
                base = (f'const auto revision="{REVISION}";\nconst auto build="{BUILD}";\n'
                        'const char* skill_damage_scope_sha256 = "aaaa";\n'
                        'constexpr auto skill_damage_scope_effects[] = {\n};\n'
                        'constexpr auto skill_damage_scope_display[] = {\n};\n')
                output = module.extend_contract(base, review)
                facts = [json.loads(json.loads(m[1])) for line in output.splitlines()
                         if (m := re.fullmatch(r'  \{\d+, "[^"]+", (".*")\},', line))]
                bindings = {p['effect_id']: p['skill_spell_ids'] for f in facts for p in f['local_skill_bindings']}
                if module is spec:
                    self.assertEqual(bindings[1357605], [441172])
                    self.assertNotIn(1361425, bindings)
                    self.assertNotIn(1361426, bindings)
                else:
                    self.assertNotIn(1357546, bindings)
                with self.assertRaises(ValueError):
                    module.extend_contract(base, {**review, 'draft': True})

    def test_selector_semantics_and_legacy_projection(self):
        current = spec.SupplementAffectedScope(self.source, self.spells)
        legacy = spec.SupplementAffectedScope(self.source, self.spells, legacy=True)
        self.assertEqual(current.affected(self.effects[1357605]), [441172])
        self.assertEqual(legacy.affected(self.effects[1357605]), [])
        e = dict(self.effects[1357605])
        for subtype in (218, 219, 320):
            e['_subtype'] = subtype
            self.assertEqual(current.affected(e), [441172])
        e['_subtype'] = 108
        self.assertEqual(current.affected(e), [])  # label is not a flags selector
        e['_subtype'] = 271
        e['_misc_value'] = 0
        self.assertEqual(current.affected(e), [441172])
        e['_subtype'] = 537
        e['_misc_value'] = 7277
        e['_misc_value_2'] = 0
        self.assertEqual(current.affected(e), [441172])
        e['_subtype'] = 271
        e['_misc_value'] = next(s['_category'] for s in self.spells.values() if s['_category'] > 0)
        e['_misc_value_2'] = -1
        self.assertEqual(current.affected(e), sorted(s['_id'] for s in self.spells.values()
                         if e['_misc_value'] > 0 and s['_category'] == e['_misc_value']))

    def test_identity_gate_is_not_relaxed(self):
        for module in (spec, mastery):
            with self.subTest(module=module.__name__):
                with patch.object(module, 'REVIEWED_REVISION', 'unapproved'), self.assertRaises(ValueError):
                    module.collect_review(self.source)
                with patch.object(module, 'REVIEWED_REVISION', REVISION), patch.object(module, 'BUILD', 'unapproved'), self.assertRaises(ValueError):
                    module.collect_review(self.source)


class LocalParameterTests(unittest.TestCase):
    def test_label_parameter_classification_and_legacy_binding_policy(self):
        for subtype in (107, 108, 218, 219, 320):
            for parameter in (0, 22, 7, 15, 1, 19, 3, 12):
                part = {'子类型': subtype, '参数': parameter, '完整DBC集合': [1]}
                self.assertEqual(spec.local_damage_component(part),
                                 subtype != 320 and parameter in (0, 22, 7, 15))
                self.assertEqual(spec.local_damage_component(part, legacy=True),
                                 subtype in (107, 108) and parameter in (0, 22, 7, 15))


if __name__ == '__main__':
    unittest.main()
