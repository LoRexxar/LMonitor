from pathlib import Path
import re

from django.test import SimpleTestCase


PATCH_DIR = Path(__file__).resolve().parents[2] / "simc_patches"
PATCH = PATCH_DIR / "0006-skill-damage-state-export.patch"
NON_FINITE_PATCH = PATCH_DIR / "0007-skill-damage-non-finite-json.patch"
DBC_UNIVERSE_PATCH = PATCH_DIR / "0008-skill-damage-dbc-universe.patch"
PRODUCT_SEMANTICS_PATCH = PATCH_DIR / "0009-skill-damage-product-semantics.patch"
RUNTIME_CONDITIONS_PATCH = PATCH_DIR / "0010-single-talent-runtime-conditions.patch"
LOW_INTRUSION_PATCH = PATCH_DIR / "0011-low-intrusion-action-universe.patch"
NO_NATIVE_FORK_PATCH = PATCH_DIR / "0012-remove-native-fork-skill-damage-probes.patch"
EXTERNAL_RECIPIENT_PATCH = PATCH_DIR / "0013-exclude-external-recipient-actions.patch"
RESIDUAL_ACTION_PATCH = PATCH_DIR / "0014-mark-residual-actions-trigger-dependent.patch"
CHILD_ACTION_PATCH = PATCH_DIR / "0015-mark-child-actions-trigger-dependent.patch"


class SimcSkillDamageExportPatchContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = PATCH.read_text(encoding="utf-8")
        cls.non_finite_text = NON_FINITE_PATCH.read_text(encoding="utf-8")
        cls.dbc_universe_text = DBC_UNIVERSE_PATCH.read_text(encoding="utf-8")
        cls.product_semantics_text = PRODUCT_SEMANTICS_PATCH.read_text(encoding="utf-8")
        cls.runtime_conditions_text = RUNTIME_CONDITIONS_PATCH.read_text(encoding="utf-8")
        cls.low_intrusion_text = LOW_INTRUSION_PATCH.read_text(encoding="utf-8")
        cls.no_native_fork_text = NO_NATIVE_FORK_PATCH.read_text(encoding="utf-8")
        cls.external_recipient_text = EXTERNAL_RECIPIENT_PATCH.read_text(encoding="utf-8")
        cls.residual_action_text = RESIDUAL_ACTION_PATCH.read_text(encoding="utf-8")
        cls.child_action_text = CHILD_ACTION_PATCH.read_text(encoding="utf-8")

    def test_cli_controls_and_early_initialized_export(self):
        for token in (
            "skill_damage_export",
            "skill_damage_revision",
            "skill_damage_game_build",
            "export_skill_damage",
        ):
            self.assertIn(token, self.text)
        self.assertIsNotNone(
            re.search(r"init\(\);.*export_skill_damage", self.text, flags=re.S)
        )
        self.assertIn("return 0", self.text)

    def test_revision_is_full_sha_and_output_is_atomic(self):
        self.assertIn("40-character hexadecimal git SHA", self.text)
        self.assertIn("std::isxdigit", self.text)
        self.assertIn(".tmp.", self.text)
        self.assertIn("::fsync", self.text)
        self.assertIn("std::rename", self.text)

    def test_schema_contract(self):
        for field in (
            "schema_version", "simc_revision", "game_build", "actors",
            "class", "spec", "name", "attributes", "primary_attribute",
            "attack_power", "spell_power", "crit", "haste", "mastery",
            "versatility", "rating", "percent", "actions", "token",
            "spell_id", "background", "harmful", "parent_token", "baseline",
            "direct_min", "direct_max", "tick", "scenarios", "buffs",
            "stacks", "delta_pct", "unresolved_reason",
        ):
            self.assertIn(f'\\"{field}\\"', self.text)

    def test_dbc_normalization_is_separate_from_runtime_amounts(self):
        for field in (
            "normalization_basis", "attack_power", "spell_power",
            "dbc_scaling", "attack_power_coefficient",
            "spell_power_coefficient", "weapon_multiplier",
            "normalized_base", "requires_weapon_data",
        ):
            self.assertIn(f'\\"{field}\\"', self.text)
        self.assertIn("attack_power = 1", self.text)
        self.assertIn("spell_power = 1", self.text)

        # Runtime snapshots remain evidence for conditional multipliers; they are
        # not the DBC-normalized base value shown by the data panel.
        for token in (
            "does_direct_damage", "does_periodic_damage",
            "calculate_direct_amount", "calculate_tick_amount", "snapshot_state",
            "direct_multiplier", "tick_multiplier",
        ):
            self.assertIn(token, self.text)

    def test_scenarios_are_apl_cooccurrence_plus_singletons_not_powerset(self):
        self.assertIn("co-occurrence sets plus singleton buffs", self.text)
        self.assertIn("never a powerset", self.text)
        self.assertIn("action_priority_list", self.text)
        self.assertIn("buff\\.", self.text)
        self.assertIn("scenario_keys.insert", self.text)
        self.assertNotIn("1 << buffs.size", self.text)

    def test_external_buffs_excluded_and_snapshots_use_caller_process_isolation(self):
        for external in ("bloodlust", "arcane_intellect", "power_word_fortitude"):
            self.assertIn(external, self.text)
        self.assertIn("source != &player", self.text)
        self.assertIn("dbc::get_class_spell_family", self.text)
        self.assertIn("class_family()", self.text)
        self.assertIn('\\"class_family\\"', self.text)

        removed_lines = "\n".join(
            line[1:] for line in self.no_native_fork_text.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        added_lines = "\n".join(
            line[1:] for line in self.no_native_fork_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        for token in ("::pipe", "::fork", "::waitpid", "::_exit"):
            self.assertIn(token, removed_lines)
            self.assertNotIn(token, added_lines)
        self.assertIn("fork_pipe_per_action_scenario", removed_lines)
        self.assertIn("actor_process_recursive_isolation", added_lines)
        self.assertIn("action.player->reset()", added_lines)
        self.assertIn("buff->trigger", added_lines)
        self.assertIn("action.sim->fixed_time = false", self.runtime_conditions_text)
        self.assertIn("action.target->resources.base[ RESOURCE_HEALTH ] = 100.0", self.runtime_conditions_text)
        self.assertIn("action.target->resources.current[ RESOURCE_HEALTH ]", self.runtime_conditions_text)
        self.assertIn("selected_trait_tokens", self.runtime_conditions_text)
        self.assertNotIn("reset_skill_damage_state", added_lines)

    def test_external_recipient_actions_are_not_probed_as_player_damage(self):
        self.assertIn("skill_damage_external_recipient_action", self.external_recipient_text)
        for token in (
            "infernos_blessing",
            "blistering_scales_damage",
            "fate_mirror_damage",
            "fate_mirror_heal",
            "breath_of_eons_damage",
            "bombardments",
        ):
            self.assertIn(token, self.external_recipient_text)
        self.assertIsNotNone(re.search(
            r"skill_damage_external_recipient_action\( \*action \)[\s\S]*?continue",
            self.external_recipient_text,
        ))

    def test_residual_actions_are_retained_but_not_standalone_probed(self):
        text = self.residual_action_text
        self.assertIn('action/residual_action.hpp', text)
        self.assertRegex(
            text,
            r'action_state_t\* state = action\.get_state\(\);[\s\S]*?'
            r'dynamic_cast<residual_action::residual_periodic_state_t\*>\( state \)[\s\S]*?'
            r'action_state_t::release\( state \);[\s\S]*?return residual;',
        )
        self.assertEqual(
            text.count(
                'else if ( residual_trigger_dependent ) '
                'baseline.unresolved_reason = "residual_trigger_dependent";'
            ),
            1,
        )
        self.assertEqual(
            text.count(
                'else if ( residual_trigger_dependent ) '
                'changed.unresolved_reason = "residual_trigger_dependent";'
            ),
            1,
        )
        self.assertIn(
            'requires_weapon_data || residual_trigger_dependent ? "false" : "true"',
            text,
        )
        self.assertRegex(
            text,
            r'else if \( residual_trigger_dependent \) '
            r'apl_metadata_json_string\( out, "residual_trigger_dependent" \);[\s\S]*?'
            r'skill_damage_dbc_scaling\( \*action \)',
        )
        self.assertNotRegex(text, r'if \( residual_trigger_dependent \)[^\n]*continue')
        self.assertNotIn('action->name_str == "ignite"', text)

    def test_child_actions_are_retained_but_not_standalone_probed(self):
        text = self.child_action_text
        self.assertRegex(
            text,
            r'std::set<const action_t\*> child_trigger_dependent_actions;[\s\S]*?'
            r'for \( const action_t\* parent_action : player->action_list \)[\s\S]*?'
            r'parent_action->child_action\.begin\(\), parent_action->child_action\.end\(\)',
        )
        self.assertRegex(
            text,
            r'const bool child_trigger_dependent = child_trigger_dependent_actions\.find\( action \) !=[\s\S]*?'
            r'child_trigger_dependent_actions\.end\(\);',
        )
        self.assertEqual(
            text.count(
                'else if ( child_trigger_dependent ) '
                'baseline.unresolved_reason = "child_trigger_dependent";'
            ),
            1,
        )
        self.assertEqual(
            text.count(
                'else if ( child_trigger_dependent ) '
                'changed.unresolved_reason = "child_trigger_dependent";'
            ),
            1,
        )
        self.assertIn(
            'requires_weapon_data || residual_trigger_dependent || child_trigger_dependent '
            '? "false" : "true"',
            text,
        )
        self.assertRegex(
            text,
            r'else if \( child_trigger_dependent \) '
            r'apl_metadata_json_string\( out, "child_trigger_dependent" \);[\s\S]*?'
            r'skill_damage_dbc_scaling\( \*action \)',
        )
        self.assertNotRegex(text, r'if \( child_trigger_dependent \)[^\n]*continue')
        self.assertNotIn('action->name_str == "earthquake_damage"', text)

    def test_export_outputs_fixed_preset_mathematical_expectation(self):
        self.assertIn('"schema_version\\\":2', self.text)
        self.assertIn('"attack_power\\\":100', self.text)
        self.assertIn('"spell_power\\\":100', self.text)
        self.assertIn('"crit_percent\\\":20', self.text)
        self.assertIn('"mastery_percent\\\":50', self.text)
        self.assertIn('state->attack_power = 100.0', self.text)
        self.assertIn('state->spell_power = 100.0', self.text)
        self.assertIn('action.calculate_crit_damage_bonus', self.text)
        self.assertIn('action.may_crit', self.text)
        self.assertIn('action.tick_may_crit', self.text)
        self.assertIn('expected', self.text)
        self.assertIn('weapon_dependent', self.text)

    def test_non_finite_runtime_amounts_are_valid_json_nulls_with_evidence(self):
        self.assertIn("std::isfinite", self.non_finite_text)
        self.assertIn("runtime_non_finite_amount", self.non_finite_text)
        self.assertIn("write_skill_damage_json_number", self.non_finite_text)
        self.assertNotIn("runtime_non_finite_amount", self.text)

    def test_changed_scenarios_and_identity_are_explicit(self):
        self.assertIn("skill_damage_amount_changed", self.text)
        self.assertIn("runtime_snapshot_probe", self.text)
        self.assertIn("unresolved_reason", self.text)
        self.assertIn("exported_actions.emplace", self.text)
        self.assertIn("immutable scenario key", self.text)
        self.assertIn("WIFSIGNALED", self.no_native_fork_text)
        self.assertIn("snapshot_child_signal_", self.no_native_fork_text)

    def test_action_universe_reads_initialized_actions_without_forcing_native_construction(self):
        for token in (
            "active_class_spell_t::data",
            "specialization_spell_entry_t::data",
            "player_traits",
            "trait_data_t::find",
            "action_names_from_spell_id",
            "skill_damage_dbc_candidates",
            "dbc_candidate_source",
            "dbc_spellbook_selected_traits_and_derived_actions",
        ):
            self.assertIn(token, self.dbc_universe_text)
        added_lines = "\n".join(
            line[1:] for line in self.low_intrusion_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removed_lines = "\n".join(
            line[1:] for line in self.low_intrusion_text.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        self.assertIn('player->create_action( action_name, "" )', removed_lines)
        self.assertIn("player->find_action( action_name )", added_lines)
        self.assertIn("dbc_action_not_initialized", added_lines)
        self.assertNotIn("create_action", added_lines)
        self.assertIsNone(
            re.search(r"\b(?:fork|pipe|waitpid|_exit)\s*\(", added_lines),
            "low-intrusion patch must not add process-isolation calls",
        )
        for forbidden in (
            "skill_damage_action_creation_failure", "WIFSIGNALED", "WNOHANG",
        ):
            self.assertNotIn(forbidden, added_lines)
        self.assertIn("std::vector<action_t*> actions", self.dbc_universe_text)
        self.assertIn("std::any_of", self.dbc_universe_text)
        self.assertNotIn("candidate.action = action;\n+          break;", self.dbc_universe_text)
        self.assertIn("INIT_ACTOR_CREATE_ACTIONS + 90", self.dbc_universe_text)
        self.assertIn("action_name == \"dismiss_pet\"", self.dbc_universe_text)
        self.assertIn("ignored_non_damage_utility && !has_non_ignored_mapping", added_lines)
        self.assertNotIn("action_priority_list", self.dbc_universe_text)

    def test_product_semantics_patch_is_incremental_after_existing_exporter_patches(self):
        added_lines = [
            line for line in self.product_semantics_text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        ]
        self.assertLess(len(added_lines), 100)
        self.assertNotIn('+void export_skill_damage( sim_t& sim )', added_lines)
        self.assertNotIn('+#include "action/action.hpp"', added_lines)

    def test_product_semantics_export_simc_native_crit_and_raw_dbc_spell_effect_scaling(self):
        added_lines = '\n'.join(
            line[1:] for line in self.product_semantics_text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        )
        self.assertIn('"schema_version\\\":3', self.product_semantics_text)
        self.assertIn('crit_multiplier', added_lines)
        self.assertIn('1.0 + state->result_crit_bonus', added_lines)
        self.assertIn('calculate_crit_damage_bonus', self.text)
        self.assertNotIn('crit / hit', added_lines)
        self.assertIn('action.data().effects()', added_lines)
        self.assertIn('effect.type() == E_SCHOOL_DAMAGE', added_lines)
        self.assertIn('effect.subtype() == A_PERIODIC_DAMAGE', added_lines)
        self.assertIn('effect.ap_coeff()', added_lines)
        self.assertIn('effect.sp_coeff()', added_lines)
        self.assertIn('\\"spell_effect\\"', added_lines)
        self.assertNotIn('action->attack_power_mod', added_lines)
        self.assertNotIn('action->spell_power_mod', added_lines)
