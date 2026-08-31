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
PARENT_STATE_ACTION_PATCH = PATCH_DIR / "0016-narrow-parent-state-dependent-actions.patch"
CAST_COMPONENT_PATCH = PATCH_DIR / "0017-skill-damage-cast-components.patch"
RUNTIME_BUFF_ACTIVATION_PATCH = PATCH_DIR / "0018-runtime-buff-activation.patch"
GLOBAL_DAMAGE_RUNTIME_PATCH = PATCH_DIR / "0019-global-damage-runtime-evidence.patch"
OWNED_CONDITION_PATCH = PATCH_DIR / "0020-owned-runtime-conditions.patch"
TALENT_EFFECTIVENESS_PATCH = PATCH_DIR / "0021-talent-effectiveness.patch"
RUNTIME_LAYER_SCENARIO_CHANGE_PATCH = PATCH_DIR / "0022-runtime-layer-scenario-change.patch"
TARGET_STATE_MATERIALIZATION_PATCH = PATCH_DIR / "0023-materialize-target-runtime-states.patch"
GLOBAL_SKILL_EFFECT_EVIDENCE_PATCH = PATCH_DIR / "0024-global-skill-effect-evidence.patch"
ACTIVE_PLAYER_SKILL_PATCH = PATCH_DIR / "0025-active-player-skill-identity.patch"
SPECIALIZATION_PASSIVE_PROVENANCE_PATCH = PATCH_DIR / "0026-specialization-passive-damage-provenance.patch"
SELECTED_TRAIT_ACTION_PROVENANCE_PATCH = PATCH_DIR / "0027-selected-trait-action-provenance.patch"
EQUIPMENT_ACTION_PROVENANCE_PATCH = PATCH_DIR / "0028-exclude-equipment-actions.patch"
RUNTIME_BUFF_STACKS_PATCH = PATCH_DIR / "0029-export-all-runtime-buff-stacks.patch"
MULTI_TARGET_DAMAGE_PATCH = PATCH_DIR / "0030-export-multi-target-skill-damage.patch"
PARTIAL_MONK_HERO_PROBE_PATCH = PATCH_DIR / "0031-allow-partial-monk-hero-talent-probes.patch"


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
        cls.parent_state_action_text = PARENT_STATE_ACTION_PATCH.read_text(encoding="utf-8")
        cls.cast_component_text = CAST_COMPONENT_PATCH.read_text(encoding="utf-8")
        cls.runtime_buff_activation_text = RUNTIME_BUFF_ACTIVATION_PATCH.read_text(encoding="utf-8")
        cls.global_damage_runtime_text = GLOBAL_DAMAGE_RUNTIME_PATCH.read_text(encoding="utf-8")
        cls.owned_condition_text = OWNED_CONDITION_PATCH.read_text(encoding="utf-8")
        cls.talent_effectiveness_text = TALENT_EFFECTIVENESS_PATCH.read_text(encoding="utf-8")
        cls.runtime_layer_scenario_change_text = RUNTIME_LAYER_SCENARIO_CHANGE_PATCH.read_text(encoding="utf-8")
        cls.target_state_materialization_text = TARGET_STATE_MATERIALIZATION_PATCH.read_text(encoding="utf-8")
        cls.global_skill_effect_evidence_text = GLOBAL_SKILL_EFFECT_EVIDENCE_PATCH.read_text(encoding="utf-8")
        cls.active_player_skill_text = ACTIVE_PLAYER_SKILL_PATCH.read_text(encoding="utf-8")
        cls.specialization_passive_provenance_text = SPECIALIZATION_PASSIVE_PROVENANCE_PATCH.read_text(encoding="utf-8")
        cls.selected_trait_action_provenance_text = SELECTED_TRAIT_ACTION_PROVENANCE_PATCH.read_text(encoding="utf-8")
        cls.equipment_action_provenance_text = EQUIPMENT_ACTION_PROVENANCE_PATCH.read_text(encoding="utf-8")
        cls.runtime_buff_stacks_text = RUNTIME_BUFF_STACKS_PATCH.read_text(encoding="utf-8")
        cls.multi_target_damage_text = MULTI_TARGET_DAMAGE_PATCH.read_text(encoding="utf-8")
        cls.partial_monk_hero_probe_text = PARTIAL_MONK_HERO_PROBE_PATCH.read_text(encoding="utf-8")

    def test_partial_monk_hero_tree_validation_is_bypassed_only_for_skill_damage_export(self):
        text = self.partial_monk_hero_probe_text
        self.assertIn('engine/class_modules/monk/sc_monk.cpp', text)
        self.assertIn(
            'if ( sim->skill_damage_export_file.empty() && count < expected && count != 0 )',
            text,
        )
        added_lines = '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        )
        self.assertNotIn('return true', added_lines)
        self.assertNotIn('return false', added_lines)
        self.assertIn('Invalid Hero Talent tree', text)

    def test_multi_target_damage_uses_native_action_state_aoe_for_five_schema_twelve_scenarios(self):
        text = self.multi_target_damage_text
        for token in (
            '\\\"schema_version\\\":12', 'scenario_counts = { 2, 5, 10, 20 }',
            'target_hit[ 1 ] = amount.direct_amount.hit',
            'target_hit[ 1 ] = amount.tick_amount.hit',
            'hit_state->n_targets', 'hit_state->chain_target', 'calculate_direct_amount',
            'calculate_tick_amount', '\\"target_hit\\"',
        ):
            self.assertIn(token, text)
        added_lines = '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        )
        for forbidden in ('warrior', 'fury', 'whirlwind', 'thunder_blast'):
            self.assertNotIn(forbidden, added_lines.lower())

    def test_runtime_buff_stacks_are_exported_as_distinct_schema_eleven_scenarios(self):
        text = self.runtime_buff_stacks_text
        for token in (
            '\\"schema_version\\\":11', 'int stacks = 1',
            'stacks <= max_stacks', 'condition.stacks',
            'max_expanded_scenarios', 'std::length_error',
        ):
            self.assertIn(token, text)
        added_lines = '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        )
        for forbidden in ('warrior', 'fury', 'overwhelmed', 'executioner'):
            self.assertNotIn(forbidden, added_lines.lower())

    def test_equipment_actions_are_excluded_by_reporting_root_item_provenance(self):
        text = self.equipment_action_provenance_text
        self.assertIn("reporting_root->item", text)
        self.assertIn('\\"schema_version\\\":10', text)
        self.assertRegex(text, r"reporting_root->item[\s\S]*?continue")
        added_lines = '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        )
        for forbidden in ('venomfang', '1306635', 'warrior', 'fury'):
            self.assertNotIn(forbidden, added_lines.lower())

    def test_selected_trait_action_provenance_is_exported_with_schema_nine(self):
        text = self.selected_trait_action_provenance_text
        for token in (
            '\\"schema_version\\\":9', 'selected_trait_effects',
            'effects_affecting_spell', 'player.player_traits', 'trait_entry_id',
            'source_spell_id', 'effect_index',
        ):
            self.assertIn(token, text)
        added_lines = '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        )
        for forbidden in ('thunder_blast', 'warrior', '435607', '435222'):
            self.assertNotIn(forbidden, added_lines.lower())

    def test_specialization_passive_provenance_is_exported_with_schema_eight(self):
        text = self.specialization_passive_provenance_text
        for token in (
            '\\"schema_version\\\":8', 'specialization_passive_effects',
            'effects_affecting_spell', 'effect_index', 'source_spell_id',
            'source_name', 'component', 'factor',
        ):
            self.assertIn(token, text)

    def test_player_skill_identity_uses_current_actor_dbc_candidates(self):
        text = self.active_player_skill_text
        self.assertIn("skill_damage_dbc_candidates.find( &player )", text)
        self.assertIn("candidate.actions", text)
        self.assertIn("reporting_root", text)
        self.assertNotIn("get_class_spell_family", '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        ))

    def test_global_skill_effect_evidence_exports_uncapped_crit_and_base_layers(self):
        text = self.global_skill_effect_evidence_text
        for token in (
            '\\"schema_version\\\":7', 'crit_chance_uncapped', 'can_crit',
            'base_damage_layers', 'base_multiplier', 'component_multiplier',
            'action_da_multiplier()', 'action_ta_multiplier()',
            '#include <limits>', 'std::numeric_limits<double>::max_digits10',
        ):
            self.assertIn(token, text)
        self.assertIn('skill_damage_amount_changed', text)
        self.assertRegex(
            text,
            re.compile(
                r'skill_damage_amount_changed[\s\S]*?'
                r'a\.direct_amount\.can_crit != b\.direct_amount\.can_crit[\s\S]*?'
                r'a\.tick_amount\.can_crit != b\.tick_amount\.can_crit'
            ),
        )

    def test_baseline_materializes_target_state_before_scenario_discovery(self):
        text = self.target_state_materialization_text
        self.assertIn("baseline = skill_damage_snapshot( *action, {} )", text)
        self.assertIn("const auto scenarios = skill_damage_scenarios( *action )", text)
        self.assertRegex(
            text,
            re.compile(
                r'else baseline = skill_damage_snapshot\( \*action, \{\} \);\n'
                r'\+      // Runtime multiplier evaluation materializes lazy actor_target_data_t states\.\n'
                r'\+      const auto scenarios = skill_damage_scenarios\( \*action \);'
            ),
        )

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

    def test_parent_state_filter_is_explicit_and_does_not_hide_all_child_damage(self):
        text = self.parent_state_action_text
        self.assertIn("snapshot_state_requires_parent_execute_state", text)
        self.assertRegex(
            text,
            r"earthquake_damage_base_t[\s\S]*?snapshot_state_requires_parent_execute_state = true;",
        )
        self.assertRegex(
            text,
            r"primordial_storm_t[\s\S]*?mw_parent = parent;[\s\S]*?snapshot_state_requires_parent_execute_state = true;",
        )
        self.assertIn(
            "const bool child_trigger_dependent = "
            "action->snapshot_state_requires_parent_execute_state;",
            text,
        )
        added_lines = "\n".join(
            line[1:] for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertNotIn("child_trigger_dependent_actions", added_lines)
        for required_child in (
            "rampage1", "rampage2", "rampage3", "rampage4",
            "odyns_fury_mh", "odyns_fury_oh", "bladestorm_mh", "bladestorm_oh",
        ):
            self.assertNotIn(required_child, text)

    def test_runtime_buff_activation_uses_observable_nonzero_duration_state(self):
        text = self.runtime_buff_activation_text
        added_lines = "\n".join(
            line[1:] for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removed_lines = "\n".join(
            line[1:] for line in text.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        self.assertIn("buff->trigger", removed_lines)
        self.assertIn("buff->execute", added_lines)
        self.assertIn("buff_t::DEFAULT_VALUE()", added_lines)
        self.assertIn("timespan_t::from_seconds( 1.0 )", added_lines)
        self.assertNotIn("override_buff", added_lines)
        self.assertNotIn("SKILL_DAMAGE_AVATAR", text)
        self.assertNotIn("std::fprintf", added_lines)

    def test_cast_component_patch_exports_reporting_root_and_periodic_total_count(self):
        text = self.cast_component_text
        for token in (
            '\\"schema_version\\\":4', 'reporting_root_token',
            'reporting_root_spell_id', 'reporting_root_component',
            'damage_equivalent_count', 'composite_dot_duration',
            'tick_time', 'last_tick_factor', 'tick_zero', 'tick_on_application',
        ):
            self.assertIn(token, text)
        self.assertIn('effect.trigger()->id() == spell_id', text)
        self.assertIn('std::map<const action_t*, std::set<const action_t*>> action_parents', text)
        self.assertIn('action_parents[ child ].insert( candidate )', text)
        self.assertIn('parent_it->second.size() != 1', text)
        self.assertIn('unambiguous_ancestry && trigger_proven ? reporting_root_candidate : action', text)
        self.assertNotIn('action_parents.emplace( child, candidate )', text)

    def test_unresolved_dbc_candidates_export_standalone_reporting_root(self):
        text = self.cast_component_text
        self.assertGreaterEqual(text.count('reporting_root_token'), 2)
        self.assertGreaterEqual(text.count('reporting_root_spell_id'), 2)
        self.assertGreaterEqual(text.count('reporting_root_component'), 2)
        self.assertIn('apl_metadata_json_string( out, token )', text)
        self.assertIn('candidate.spell_id', text)
        self.assertIn(
            'if ( exported_actions.find( { token, candidate.spell_id } ) != exported_actions.end() )\n+          continue;',
            text,
        )

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

    def test_scenario_change_detection_includes_all_runtime_layers(self):
        text = self.runtime_layer_scenario_change_text
        self.assertIn("layers_changed", text)
        for field in (
            "action_multiplier", "player_multiplier", "versus_multiplier",
            "persistent_multiplier", "target_multiplier", "versatility",
            "pet_multiplier", "target_pet_multiplier",
        ):
            self.assertIn(f"changed( x.{field}, y.{field} )", text)
        self.assertIn("a.direct_amount.runtime_layers, b.direct_amount.runtime_layers", text)
        self.assertIn("a.tick_amount.runtime_layers, b.tick_amount.runtime_layers", text)

    def test_runtime_layer_components_fail_closed_on_non_positive_values(self):
        text = self.owned_condition_text
        self.assertIn("skill_damage_runtime_layers_valid", text)
        for field in (
            "action_multiplier", "player_multiplier", "versus_multiplier",
            "persistent_multiplier", "target_multiplier", "versatility",
            "pet_multiplier", "target_pet_multiplier",
        ):
            self.assertIn(f"positive( layers.{field} )", text)
        self.assertGreaterEqual(text.count("runtime_layer_non_positive"), 2)
        self.assertIn("amount.direct_amount.present = false", text)
        self.assertIn("amount.direct = false", text)
        self.assertIn("amount.tick_amount.present = false", text)
        self.assertIn("amount.periodic = false", text)

    def test_owned_runtime_conditions_cover_self_buffs_and_target_debuffs_without_name_guesses(self):
        text = self.owned_condition_text
        added_lines = '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('+') and not line.startswith('+++')
        )
        removed_lines = '\n'.join(
            line[1:] for line in text.splitlines()
            if line.startswith('-') and not line.startswith('---')
        )
        for token in (
            'skill_damage_condition_t', 'skill_damage_condition_scope_e',
            'action.player->buff_list', 'action.target->buff_list',
            'buff->source != action.player', 'scope == skill_damage_condition_scope_e::TARGET',
            'buff->player != action.target',
        ):
            self.assertIn(token, added_lines)
        self.assertIn('skill_damage_amount_changed', self.text)
        self.assertIn('selected_trait_tokens', removed_lines)
        self.assertIn('talents_', removed_lines)
        self.assertNotIn('bloodlust', added_lines)
        self.assertNotIn('1 <<', added_lines)
        self.assertNotIn('skill_damage_selected_trait_spell_graph', added_lines)
        self.assertNotIn('selected_spell_ids.count', added_lines)
        self.assertIn(
            'action.player->find_action( action.name_str ) == &action',
            added_lines,
        )
        self.assertIn(
            'condition.buff->current_stack = std::max( 1, condition.buff->max_stack() )',
            added_lines,
        )
        self.assertIn('condition.buff->current_value = condition.buff->default_value', added_lines)
        self.assertIn('condition.buff->current_stack = 0', added_lines)
        self.assertIn('condition.buff->current_value = 0.0', added_lines)
        self.assertNotIn('condition.buff->execute(', added_lines)
        self.assertNotIn('condition.buff->trigger(', added_lines)
        self.assertNotIn('condition.buff->override_buff(', added_lines)
        self.assertNotIn('condition.buff->expire()', added_lines)
        self.assertNotIn('condition.buff->reset()', added_lines)

    def test_global_damage_runtime_evidence_uses_generic_player_skill_action_layers(self):
        text = self.global_damage_runtime_text
        added_lines = "\n".join(
            line[1:] for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        for token in (
            "player_skill",
            "skill_damage_runtime_layers_t",
            "runtime_layers",
            "da_multiplier", "ta_multiplier", "player_multiplier",
            "versus_multiplier", "persistent_multiplier", "versatility",
            "target_da_multiplier", "target_ta_multiplier",
            "skill_damage_player_skill",
            "dbc::get_class_spell_family",
            "reporting_root->data().class_family()",
        ):
            self.assertIn(token, added_lines)
        self.assertNotRegex(
            added_lines.lower(),
            r"avatar|weapon_specialization|hunter|warrior|talent_id|spell_id\s*==",
        )
        self.assertNotIn("composite_player_multiplier", added_lines)
        self.assertNotIn("composite_player_target_multiplier", added_lines)

    def test_talent_effectiveness_is_exported_from_actor_protocol_and_runtime_traits(self):
        text = self.talent_effectiveness_text
        added_lines = "\n".join(
            line[1:] for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertIn('"schema_version\\\":6', text)
        self.assertIn('skill_damage_talent_effectiveness', added_lines)
        self.assertIn('skill_damage_reference_', added_lines)
        self.assertIn('skill_damage_talent_', added_lines)
        self.assertIn('_trait_', added_lines)
        self.assertIn('player.player_traits', added_lines)
        self.assertIn('std::get<1>( player_trait )', added_lines)
        self.assertIn('std::get<2>( player_trait )', added_lines)
        self.assertIn('\\"talent_effectiveness\\"', added_lines)
        self.assertNotRegex(
            added_lines.lower(),
            r'avatar|haunt|warrior|warlock|talent_name|spell_id\s*==',
        )

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
