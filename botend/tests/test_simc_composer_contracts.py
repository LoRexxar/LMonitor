"""SimC Composer 当前语义契约测试。

这些测试只验证 Composer 的语义槽解析和模板渲染。任务/Worker 的执行契约由
reference worker/API 测试覆盖；本模块不再制造保存正文、hash 或 manifest 的冻结任务。
"""
from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import SimcApl, SimcContentTemplate
from botend.services.simc_composer import SimcComposer


class ComposerTestCase(TestCase):
    spec = "fury"
    spec_key = "warrior_fury"
    class_name = "warrior"

    def setUp(self):
        self.user = User.objects.create_user(username=self.id().replace(".", "_")[-120:])

    def template(self, content="{player_identity}\n{talents}\n{equipment}\n{action_list}\n{output_options}", **kwargs):
        defaults = {
            "template_type": SimcContentTemplate.TYPE_BASE_TEMPLATE,
            "source": SimcContentTemplate.SOURCE_USER,
            "spec": self.spec_key,
            "content": content,
            "is_active": True,
        }
        defaults.update(kwargs)
        return SimcContentTemplate.objects.create(**defaults)

    def default_equipment(self, content=None, **kwargs):
        defaults = {
            "template_type": SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            "source": SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            "spec": self.spec_key,
            "class_name": self.class_name,
            "content": content or 'warrior="TemplateActor"\nspec=fury\nhead=,id=999999',
            "is_active": True,
        }
        defaults.update(kwargs)
        return SimcContentTemplate.objects.create(**defaults)

    def apl(self, content="actions=/bloodthirst", **kwargs):
        defaults = {
            "name": "Default APL",
            "spec": self.spec_key,
            "class_name": self.class_name,
            "content": content,
            "source": "simc_upstream",
            "is_system": True,
            "is_active": True,
        }
        defaults.update(kwargs)
        return SimcApl.objects.create(**defaults)

    def compose(self, template, **overrides):
        request = {
            "spec": self.spec,
            "base_template_id": template.id,
            "player_import_mode": "manual_equipment",
            "player_equipment": f'{self.class_name}="Player"\nspec={self.spec}\nhead=,id=212048',
            "_result_file_path": "simc/result.html",
        }
        request.update(overrides)
        return SimcComposer(self.user.id).compose(request)


class SimcComposerEquipmentSlotResolutionTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.template()
        self.default_equipment()

    def test_manual_equipment_blocks_default_equipment(self):
        final, manifest, error = self.compose(self.base)
        self.assertIsNone(error)
        self.assertIn("id=212048", final)
        self.assertNotIn("id=999999", final)
        self.assertEqual(manifest.slots["equipment"]["source"], "manual_equipment")

    def test_addon_export_blocks_default_and_is_split_into_slots(self):
        export = (
            'warrior="AddonPlayer"\nspec=fury\nlevel=80\nrace=orc\n'
            "talents=BUILD\nhead=,id=111111\nactions=/charge"
        )
        final, manifest, error = self.compose(
            self.base, player_import_mode="addon_full_export", player_equipment=export
        )
        self.assertIsNone(error)
        self.assertIn('warrior="AddonPlayer"', final)
        self.assertIn("talents=BUILD", final)
        self.assertIn("id=111111", final)
        self.assertIn("actions=/charge", final)
        self.assertNotIn("id=999999", final)
        for slot in ("player_identity", "talents", "equipment", "action_list"):
            self.assertEqual(manifest.slots[slot]["source"], "addon_export")
        self.assertEqual(sum(line.startswith("head=") for line in final.splitlines()), 1)

    def test_empty_armory_equipment_occupies_slot_without_fallback(self):
        final, manifest, error = self.compose(
            self.base,
            player_import_mode="battlenet",
            player_equipment="",
            battlenet_region="us",
            battlenet_realm="area-52",
            battlenet_character="testchar",
            _server_preflight={"character": {"class": "warrior", "spec": "fury"}},
        )
        self.assertIsNone(error)
        self.assertIn("armory=us,area-52,testchar", final)
        self.assertNotIn("id=999999", final)
        self.assertEqual(manifest.slots["equipment"]["source"], "battlenet_armory")
        self.assertEqual(manifest.slots["equipment"]["status"], "empty")


class SimcComposerIdentitySlotResolutionTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.template()

    def test_matching_battlenet_identity_composes_one_armory_actor(self):
        final, _, error = self.compose(
            self.base,
            player_import_mode="battlenet",
            player_equipment="",
            battlenet_region="us",
            battlenet_realm="area-52",
            battlenet_character="testchar",
            _server_preflight={"character": {"class": "warrior", "spec": "fury"}},
        )
        self.assertIsNone(error)
        self.assertEqual([line for line in final.splitlines() if line.startswith("armory=")],
                         ["armory=us,area-52,testchar"])

    def test_battlenet_identity_replaces_static_actor_in_legacy_base_template(self):
        self.base.content = (
            'warrior="LMonitor_SimC"\n'
            'spec={spec}\n'
            'fight_style={fight_style}\n'
            'max_time={time}\n'
            'html={result_file}\n'
            'desired_targets={target_count}\n\n'
            '{player_config}\n\n'
            '{action_list}'
        )
        self.base.save(update_fields=["content"])
        final, _, error = self.compose(
            self.base,
            player_import_mode="battlenet",
            player_equipment="",
            battlenet_region="eu",
            battlenet_realm="Blackmoore",
            battlenet_character="Zornfalte",
            _server_preflight={"character": {"class": "warrior", "spec": "fury"}},
        )
        self.assertIsNone(error)
        self.assertIn("armory=eu,Blackmoore,Zornfalte", final)
        self.assertNotIn('warrior="LMonitor_SimC"', final)

    def test_battlenet_spec_conflict_is_rejected(self):
        final, manifest, error = self.compose(
            self.base,
            spec="arms",
            player_import_mode="battlenet",
            player_equipment="",
            battlenet_region="us",
            battlenet_realm="area-52",
            battlenet_character="testchar",
            _server_preflight={"character": {"class": "warrior", "spec": "fury"}},
        )
        self.assertIsNone(final)
        self.assertIsNone(manifest)
        self.assertIn("冲突", error)

    def test_battlenet_class_conflict_is_rejected(self):
        final, _, error = self.compose(
            self.base,
            spec="fire",
            player_import_mode="battlenet",
            player_equipment="",
            battlenet_region="us",
            battlenet_realm="area-52",
            battlenet_character="testchar",
            _server_preflight={"character": {"class": "warrior", "spec": "fire"}},
        )
        self.assertIsNone(final)
        self.assertIn("冲突", error)

    def test_export_spec_conflict_is_rejected(self):
        final, _, error = self.compose(
            self.base,
            spec="arms",
            player_import_mode="addon_full_export",
            player_equipment='warrior="Player"\nspec=fury\nhead=,id=1',
        )
        self.assertIsNone(final)
        self.assertIn("冲突", error)


class SimcComposerAplSlotResolutionTests(ComposerTestCase):
    def test_explicit_empty_apl_does_not_fall_back(self):
        base = self.template()
        selected = self.apl("actions=/rampage")
        final, manifest, error = self.compose(
            base, selected_apl_id=selected.id, override_action_list=""
        )
        self.assertIsNone(error)
        self.assertNotIn("rampage", final)
        self.assertEqual(manifest.slots["action_list"]["source"], "user_explicit_empty")
        self.assertEqual(manifest.slots["action_list"]["status"], "explicit_empty")

    def test_selected_apl_is_resolved_by_reference(self):
        base = self.template()
        selected = self.apl("actions=/execute", source="user", is_system=False,
                            owner_user_id=self.user.id)
        final, manifest, error = self.compose(base, selected_apl_id=selected.id)
        self.assertIsNone(error)
        self.assertIn("actions=/execute", final)
        self.assertEqual(manifest.slots["action_list"]["source_id"], selected.id)

    def test_other_users_private_apl_is_not_resolved(self):
        base = self.template()
        other = User.objects.create_user(username="other_apl_owner")
        selected = self.apl(source="user", is_system=False, owner_user_id=other.id)
        composer = SimcComposer(self.user.id)
        result = composer._resolve_action_list({"spec": "fury", "selected_apl_id": selected.id})
        self.assertEqual(result.status, "missing")
        self.assertIn("无权访问", result.error)


class SimcComposerTemplateRenderingTests(ComposerTestCase):
    def test_all_supported_placeholders_are_replaced(self):
        base = self.template(
            "fight_style={fight_style}\nmax_time={time}\ndesired_targets={target_count}\n"
            "{simulation_options}\n{player_identity}\n{talents}\n{equipment}\n"
            "{stat_overrides}\n{action_list}\n{output_options}"
        )
        final, _, error = self.compose(
            base, fight_style="Patchwerk", time=300, target_count=1,
            override_action_list="actions=/execute", gear_crit=123,
        )
        self.assertIsNone(error)
        for placeholder in ("{fight_style}", "{time}", "{target_count}",
                            "{simulation_options}", "{player_identity}", "{talents}",
                            "{equipment}", "{stat_overrides}", "{action_list}",
                            "{output_options}"):
            self.assertNotIn(placeholder, final)
        self.assertIn("gear_crit_rating=123", final)
        self.assertIn("html=simc/result.html", final)

    def test_final_content_has_one_actor_and_one_spec(self):
        final, _, error = self.compose(self.template())
        self.assertIsNone(error)
        actor_lines = [line for line in final.splitlines() if line.startswith('warrior=')]
        spec_lines = [line for line in final.splitlines() if line.startswith("spec=")]
        self.assertEqual(len(actor_lines), 1)
        self.assertEqual(len(spec_lines), 1)

    def test_unknown_placeholder_is_rejected(self):
        final, manifest, error = self.compose(
            self.template("{player_identity}\n{equipment}\n{unknown_placeholder}")
        )
        self.assertIsNone(final)
        self.assertIsNone(manifest)
        self.assertIn("{unknown_placeholder}", error)

    def test_output_is_appended_when_legacy_template_omits_output_slot(self):
        final, _, error = self.compose(self.template("{player_identity}\n{equipment}"))
        self.assertIsNone(error)
        self.assertEqual([line for line in final.splitlines() if line.startswith("html=")],
                         ["html=simc/result.html"])


class SimcComposerReferenceAccessTests(ComposerTestCase):
    def test_invalid_explicit_template_reference_fails_closed(self):
        missing = SimcContentTemplate(id=999999)
        final, manifest, error = self.compose(missing)
        self.assertIsNone(final)
        self.assertIsNone(manifest)
        self.assertIn("未找到", error)

    def test_other_users_private_template_is_not_resolved(self):
        other = User.objects.create_user(username="other_template_owner")
        private = self.template(owner_user_id=other.id)
        final, _, error = self.compose(private)
        self.assertIsNone(final)
        self.assertIn("未找到", error)

    def test_global_template_is_resolved(self):
        global_template = self.template(owner_user_id=None)
        final, manifest, error = self.compose(global_template)
        self.assertIsNone(error)
        self.assertIsNotNone(final)
        self.assertEqual(manifest.base_template_id, global_template.id)


class SimcComposerNonWarriorSpecTests(ComposerTestCase):
    spec = "fire"
    spec_key = "mage_fire"
    class_name = "mage"

    def test_mage_defaults_are_selected_by_spec_reference(self):
        base = self.template()
        equipment = self.default_equipment(
            'mage="DefaultMage"\nspec=fire\nhead=,id=777701'
        )
        apl = self.apl("actions=/fireball")
        final, manifest, error = self.compose(
            base, player_import_mode="attribute_only", player_equipment=""
        )
        self.assertIsNone(error)
        self.assertIn('mage="DefaultMage"', final)
        self.assertEqual(sum(line.startswith('mage=') for line in final.splitlines()), 1)
        self.assertIn("id=777701", final)
        self.assertIn("actions=/fireball", final)
        self.assertEqual(manifest.slots["equipment"]["source_id"], equipment.id)
        self.assertEqual(manifest.slots["action_list"]["source_id"], apl.id)
