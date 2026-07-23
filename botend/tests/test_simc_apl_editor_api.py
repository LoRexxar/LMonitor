import json
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from botend.models import SimcAplSymbol, SimcProfile, SimcBackendBinary, WowSpellSnapshot


class SimcAplEditorApiTests(TestCase):
    REVISION = "a" * 40

    def setUp(self):
        self.user = User.objects.create_user(username="editor", password="password")
        self.client.force_login(self.user)

    def test_editor_language_api_requires_current_spec_symbol_binding(self):
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=23881,
            name="Bloodthirst", name_zh="嗜血", snapshot_build="12.0.5")
        apl = "actions+=/bloodthirst,if=target.health.pct<20"

        for conversion_type, text in (
                ("apl_to_cn", apl),
                ("cn_to_apl", "actions+=/嗜血,if=target.health.pct<20")):
            with self.subTest(conversion_type=conversion_type):
                response = self.client.post("/api/convert-text/", data=json.dumps({
                    "text": text, "conversion_type": conversion_type,
                    "spec": "warrior_fury",
                }), content_type="application/json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["result"], text)

    def test_editor_language_api_converts_the_same_document_in_both_directions(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=23881,
            name="Bloodthirst", name_zh="嗜血", snapshot_build=build)
        SimcAplSymbol.objects.create(
            simc_revision=revision, wow_build=build, class_name='warrior', spec='fury',
            token='bloodthirst', symbol_kind='action', spell_id=23881)
        apl = "actions+=/bloodthirst,if=target.health.pct<20"

        chinese = self.client.post("/api/convert-text/", data=json.dumps({
            "text": apl, "conversion_type": "apl_to_cn", "spec": "warrior_fury",
        }), content_type="application/json")
        self.assertEqual(chinese.status_code, 200)
        self.assertEqual(chinese.json(), {
            "success": True,
            "result": "actions+=/嗜血,if=target.health.pct<20",
        })

        authoritative = self.client.post("/api/convert-text/", data=json.dumps({
            "text": chinese.json()["result"], "conversion_type": "cn_to_apl",
            "spec": "warrior_fury",
        }), content_type="application/json")
        self.assertEqual(authoritative.status_code, 200)
        self.assertEqual(authoritative.json(), {"success": True, "result": apl})

    def test_editor_language_api_keeps_ambiguous_chinese_names_as_tokens(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.bulk_create([
            WowSpellSnapshot(
                branch='wow', locale='zhCN', spell_id=49998,
                name='Death Strike', name_zh='灵界打击', snapshot_build=build),
            WowSpellSnapshot(
                branch='wow', locale='zhCN', spell_id=45470,
                name='Death Strike Heal', name_zh='灵界打击', snapshot_build=build),
        ])
        SimcAplSymbol.objects.bulk_create([
            SimcAplSymbol(
                simc_revision=revision, wow_build=build, class_name='deathknight', spec='blood',
                class_key='deathknight', spec_key='blood',
                token='death_strike', symbol_kind='action', spell_id=49998),
            SimcAplSymbol(
                simc_revision=revision, wow_build=build, class_name='deathknight', spec='blood',
                class_key='deathknight', spec_key='blood',
                token='death_strike_heal', symbol_kind='action', spell_id=45470),
        ])
        apl = 'actions=/death_strike\nactions+=/death_strike_heal'

        translated = self.client.post('/api/convert-text/', data=json.dumps({
            'text': apl, 'conversion_type': 'apl_to_cn', 'spec': 'deathknight_blood',
        }), content_type='application/json')
        restored = self.client.post('/api/convert-text/', data=json.dumps({
            'text': translated.json()['result'], 'conversion_type': 'cn_to_apl',
            'spec': 'deathknight_blood',
        }), content_type='application/json')

        self.assertEqual(translated.json()['result'], apl)
        self.assertEqual(restored.json()['result'], apl)

    @override_settings(SIMC_APL_CURRENT_IDENTITY=(REVISION, "12.0.5"))
    def test_editor_language_api_reuses_same_class_exact_token_spell_binding(self):
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', snapshot_build='12.0.5',
            spell_id=6343, name='Thunder Clap', name_zh='雷霆一击',
        )
        SimcAplSymbol.objects.bulk_create([
            SimcAplSymbol(
                simc_revision=self.REVISION, wow_build='12.0.5',
                class_name='warrior', class_key='warrior',
                spec='fury', spec_key='fury', token='thunder_clap',
                symbol_kind='action', spell_id=None,
            ),
            SimcAplSymbol(
                simc_revision=self.REVISION, wow_build='12.0.5',
                class_name='warrior', class_key='warrior',
                spec='protection', spec_key='protection', token='thunder_clap',
                symbol_kind='action', spell_id=6343,
            ),
            SimcAplSymbol(
                simc_revision=self.REVISION, wow_build='12.0.5',
                class_name='warrior', class_key='warrior',
                spec='fury', spec_key='fury', hero_tree='slayer', hero_tree_key='slayer',
                token='thunder_clap', symbol_kind='action', spell_id=99999,
            ),
        ])
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', snapshot_build='12.0.5',
            spell_id=99999, name='Wrong Hero Action', name_zh='错误英雄技能',
        )

        translated = self.client.post('/api/convert-text/', data=json.dumps({
            'text': 'actions=/thunder_clap',
            'conversion_type': 'apl_to_cn',
            'spec': 'warrior_fury',
        }), content_type='application/json')

        self.assertEqual(translated.status_code, 200)
        self.assertEqual(translated.json()['result'], 'actions=/雷霆一击')

    def test_editor_language_api_preserves_document_edge_whitespace(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=23881,
            name='Bloodthirst', name_zh='嗜血', snapshot_build=build)
        SimcAplSymbol.objects.create(
            simc_revision=revision, wow_build=build,
            class_name='warrior', class_key='warrior', spec='fury', spec_key='fury',
            token='bloodthirst', symbol_kind='action', spell_id=23881)
        apl = '  actions+=/bloodthirst\n\n'

        chinese = self.client.post('/api/convert-text/', data=json.dumps({
            'text': apl, 'conversion_type': 'apl_to_cn', 'spec': 'warrior_fury',
        }), content_type='application/json')
        restored = self.client.post('/api/convert-text/', data=json.dumps({
            'text': chinese.json()['result'], 'conversion_type': 'cn_to_apl',
            'spec': 'warrior_fury',
        }), content_type='application/json')

        self.assertEqual(chinese.status_code, 200)
        self.assertEqual(chinese.json()['result'], '  actions+=/嗜血\n\n')
        self.assertEqual(restored.json()['result'], apl)

    def test_editor_language_api_keeps_one_token_with_multiple_chinese_names_unchanged(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.bulk_create([
            WowSpellSnapshot(branch='wow', locale='zhCN', spell_id=1001,
                             name='Variant One', name_zh='变体一', snapshot_build=build),
            WowSpellSnapshot(branch='wow', locale='zhCN', spell_id=1002,
                             name='Variant Two', name_zh='变体二', snapshot_build=build),
        ])
        SimcAplSymbol.objects.bulk_create([
            SimcAplSymbol(simc_revision=revision, wow_build=build,
                          class_name='warrior', class_key='warrior',
                          spec='fury', spec_key='fury',
                          token='shared_action', symbol_kind='action', spell_id=1001),
            SimcAplSymbol(simc_revision=revision, wow_build=build,
                          class_name='warrior', class_key='warrior', spec=None,
                          token='shared_action', symbol_kind='action', spell_id=1002),
        ])

        apl = 'actions=/shared_action'
        response = self.client.post('/api/convert-text/', data=json.dumps({
            'text': apl, 'conversion_type': 'apl_to_cn', 'spec': 'warrior_fury',
        }), content_type='application/json')

        self.assertEqual(response.json()['result'], apl)

    def test_editor_language_api_queries_wago_only_for_current_catalog_spell_ids(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.bulk_create([
            WowSpellSnapshot(
                branch='wow', locale='zhCN', spell_id=23881,
                name='Bloodthirst', name_zh='嗜血', snapshot_build=build),
            WowSpellSnapshot(
                branch='wow', locale='zhCN', spell_id=99999,
                name='Unrelated', name_zh='无关技能', snapshot_build=build),
        ])
        SimcAplSymbol.objects.create(
            simc_revision=revision, wow_build=build, class_name='warrior', spec='fury',
            token='bloodthirst', symbol_kind='action', spell_id=23881)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.post('/api/convert-text/', data=json.dumps({
                'text': 'actions=/bloodthirst', 'conversion_type': 'apl_to_cn',
                'spec': 'warrior_fury',
            }), content_type='application/json')

        self.assertEqual(response.json()['result'], 'actions=/嗜血')
        snapshot_queries = [query['sql'] for query in captured.captured_queries
                            if 'wow_spell_snapshot' in query['sql']]
        self.assertTrue(snapshot_queries)
        self.assertTrue(all('23881' in query and '99999' not in query
                            for query in snapshot_queries))

    def test_editor_language_api_prefers_live_wago_bilingual_names_over_legacy_pairs(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=23881,
            name="Bloodthirst", name_zh="嗜血", snapshot_build=build,
        )
        SimcAplSymbol.objects.create(
            simc_revision=revision, wow_build=build, class_name='warrior', spec='fury',
            token='bloodthirst', symbol_kind='action', spell_id=23881)
        WowSpellSnapshot.objects.create(
            branch="wowt", locale="zhCN", spell_id=23881,
            name="Bloodthirst", name_zh="测试服嗜血", snapshot_build="12.1.0",
        )

        chinese = self.client.post("/api/convert-text/", data=json.dumps({
            "text": "actions+=/bloodthirst", "conversion_type": "apl_to_cn",
            "spec": "warrior_fury",
        }), content_type="application/json")
        authoritative = self.client.post("/api/convert-text/", data=json.dumps({
            "text": "actions+=/嗜血", "conversion_type": "cn_to_apl",
            "spec": "warrior_fury",
        }), content_type="application/json")

        self.assertEqual(chinese.status_code, 200)
        self.assertEqual(chinese.json()["result"], "actions+=/嗜血")
        self.assertEqual(authoritative.status_code, 200)
        self.assertEqual(authoritative.json()["result"], "actions+=/bloodthirst")

    def test_editor_language_api_uses_only_current_spec_symbols_for_multiple_apls(self):
        revision, build = 'a' * 40, '12.0.7.68453'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        fixtures = (
            ('warrior', 'fury', 'warrior_fury', 'bloodthirst', 23881, 'Bloodthirst', '嗜血'),
            ('mage', 'arcane', 'mage_arcane', 'arcane_blast', 30451, 'Arcane Blast', '奥术冲击'),
        )
        for class_name, spec, _spec_key, token, spell_id, english, chinese in fixtures:
            WowSpellSnapshot.objects.create(
                branch='wow', locale='zhCN', spell_id=spell_id,
                name=english, name_zh=chinese, snapshot_build=build)
            SimcAplSymbol.objects.create(
                simc_revision=revision, wow_build=build, class_name=class_name,
                spec=spec, token=token, symbol_kind='action', spell_id=spell_id)

        for _class_name, _spec, spec_key, token, _spell_id, _english, chinese in fixtures:
            apl = f'actions+=/{token},if=target.health.pct<20'
            with self.subTest(spec=spec_key):
                translated = self.client.post('/api/convert-text/', data=json.dumps({
                    'text': apl, 'conversion_type': 'apl_to_cn', 'spec': spec_key,
                }), content_type='application/json')
                self.assertEqual(translated.status_code, 200)
                self.assertEqual(
                    translated.json()['result'],
                    f'actions+=/{chinese},if=target.health.pct<20')
                restored = self.client.post('/api/convert-text/', data=json.dumps({
                    'text': translated.json()['result'], 'conversion_type': 'cn_to_apl',
                    'spec': spec_key,
                }), content_type='application/json')
                self.assertEqual(restored.status_code, 200)
                self.assertEqual(restored.json()['result'], apl)

    def test_editor_language_api_treats_apl_underscores_and_spaces_as_equivalent(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=30451,
            name="Arcane Blast", name_zh="奥术冲击", snapshot_build=build)
        SimcAplSymbol.objects.create(
            simc_revision=revision, wow_build=build, class_name='mage', spec='arcane',
            token='arcane_blast', symbol_kind='action', spell_id=30451)

        for apl_variant in ("actions=/arcane_blast", "actions=/arcane blast"):
            with self.subTest(apl_variant=apl_variant):
                response = self.client.post("/api/convert-text/", data=json.dumps({
                    "text": apl_variant, "conversion_type": "apl_to_cn",
                    "spec": "mage_arcane",
                }), content_type="application/json")
                self.assertEqual(response.json()["result"], "actions=/奥术冲击")

        for chinese_variant in ("actions=/奥术冲击", "actions=/奥术 冲击"):
            with self.subTest(chinese_variant=chinese_variant):
                response = self.client.post("/api/convert-text/", data=json.dumps({
                    "text": chinese_variant, "conversion_type": "cn_to_apl",
                    "spec": "mage_arcane",
                }), content_type="application/json")
                self.assertEqual(response.json()["result"], "actions=/arcane_blast")

    def test_editor_language_api_does_not_rewrite_comments(self):
        revision, build = 'a' * 40, '12.0.5'
        SimcBackendBinary.objects.create(platform='linux64', current_version=revision)
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=30451,
            name='Arcane Blast', name_zh='奥术冲击', snapshot_build=build)
        SimcAplSymbol.objects.create(
            simc_revision=revision, wow_build=build, class_name='mage', spec='arcane',
            token='arcane_blast', symbol_kind='action', spell_id=30451)
        source = '# use arcane blast here\nactions=/arcane_blast'

        response = self.client.post('/api/convert-text/', data=json.dumps({
            'text': source, 'conversion_type': 'apl_to_cn', 'spec': 'mage_arcane',
        }), content_type='application/json')

        self.assertEqual(response.json()['result'], '# use arcane blast here\nactions=/奥术冲击')

    def test_validation_echoes_document_version_and_uses_stable_one_based_ranges(self):
        response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({
            "content": "not an apl line", "spec": "warrior_fury", "mode": "structural",
            "document_version": {"client": 17},
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"]["document_version"], {"client": 17})
        self.assertEqual(payload["data"]["diagnostics"][0]["range"], {
            "start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 16},
        })
        self.assertEqual(payload["data"]["range_contract"], {
            "base": 1, "end": "exclusive", "unit": "unicode_code_point",
        })

    def test_editor_endpoints_return_json_401_to_anonymous_users(self):
        for method, path in (("post", "/api/simc-workbench/apl-validation/"),
                             ("post", "/api/simc-workbench/apl-completions/"),
                             ("get", "/api/simc-workbench/apl-symbols/?spec=warrior_fury"),
                             ("get", "/api/simc-workbench/apl-spells/?spec=warrior_fury")):
            client = Client()
            response = (client.get(path) if method == "get" else
                        client.post(path, data="{}", content_type="application/json"))
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response["Content-Type"], "application/json")
            self.assertEqual(response.json()["error"]["code"], "authentication_required")

    def test_editor_endpoints_return_json_405_for_unsupported_methods(self):
        for path in (
                "/api/simc-workbench/apl-validation/",
                "/api/simc-workbench/apl-completions/",
                "/api/simc-workbench/apl-symbols/?spec=warrior_fury",
                "/api/simc-workbench/apl-spells/?spec=warrior_fury"):
            response = self.client.put(path, data="{}", content_type="application/json")
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response["Content-Type"], "application/json")
            self.assertEqual(response.json()["error"]["code"], "method_not_allowed")

    def test_completion_rejects_positions_outside_document(self):
        for position in ({"line": 0, "column": 1}, {"line": 3, "column": 1}, {"line": 1, "column": 5}, {"line": 1, "column": 0}):
            response = self.client.post("/api/simc-workbench/apl-completions/", data=json.dumps({"content": "act\nsecond", "position": position, "spec": "warrior_fury"}), content_type="application/json")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_position")

    def test_symbols_use_current_backend_revision(self):
        SimcAplSymbol.objects.create(simc_revision="old", wow_build="old", class_name="warrior", spec="fury", token="old", symbol_kind="action")
        SimcAplSymbol.objects.create(simc_revision=self.REVISION, wow_build="current", class_name="warrior", spec="fury", token="current", symbol_kind="action")
        SimcBackendBinary.objects.create(platform="linux64", current_version=self.REVISION)
        response = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury")
        self.assertEqual(response.json()["data"]["items"][0]["simc_revision"], self.REVISION)

    def test_validation_rejects_profile_from_another_user(self):
        other = User.objects.create_user(username="other", password="password")
        profile = SimcProfile.objects.create(user_id=other.id, name="secret", spec="fury")
        response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({
            "content": "actions=/auto_attack", "spec": "warrior_fury", "profile_id": profile.id,
            "document_version": "opaque-v1",
        }), content_type="application/json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "profile_not_found")
        self.assertNotIn("secret", response.content.decode())

    def test_validation_rejects_profile_specialization_mismatch(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name="arms", spec="arms", is_active=True)
        response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({
            "content": "actions=/auto_attack", "spec": "warrior_fury", "profile_id": profile.id,
            "document_version": "opaque-v1",
        }), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "profile_spec_mismatch")

    def test_symbols_reject_catalogs_without_an_explicit_current_identity(self):
        SimcAplSymbol.objects.create(simc_revision="arbitrary-old", wow_build="11.0.0",
            class_name="warrior", spec="fury", token="stale", symbol_kind="action")

        response = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"]["code"], "catalog_unavailable")
        self.assertNotIn("stale", response.content.decode())

    @override_settings(SIMC_APL_CURRENT_IDENTITY=("current", "12.0.5"))
    def test_configured_catalog_identity_must_use_full_git_sha(self):
        response = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "catalog_unavailable")

    def test_symbols_reject_ambiguous_active_builds_for_current_revision(self):
        SimcBackendBinary.objects.create(platform="linux64", current_version=self.REVISION)
        for wow_build in ("11.1.0", "11.2.0"):
            SimcAplSymbol.objects.create(simc_revision=self.REVISION, wow_build=wow_build,
                class_name="warrior", spec="fury", token="bloodthirst", symbol_kind="action")

        response = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"]["code"], "catalog_unavailable")

    def _catalog(self):
        for token, kind, spell_id in (("bloodthirst", "action", 23881), ("rampage", "action", 184367), ("rage", "resource", None)):
            SimcAplSymbol.objects.create(simc_revision=self.REVISION, wow_build="11.2.0",
                class_name="warrior", spec="fury", token=token, symbol_kind=kind, spell_id=spell_id,
                source="simc_manifest")
        SimcBackendBinary.objects.create(platform="linux64", current_version=self.REVISION)
        WowSpellSnapshot.objects.create(locale="enUS", spell_id=23881, name="Bloodthirst", snapshot_build="11.2.0")
        WowSpellSnapshot.objects.create(locale="zhCN", spell_id=23881, name_zh="嗜血", description="中文说明", snapshot_build="11.2.0")

    def test_symbols_filter_search_all_public_fields_and_paginate(self):
        self._catalog()
        first = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury&kind=action&page=1&page_size=1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["data"]["pagination"], {"page": 1, "page_size": 1, "total": 2, "total_pages": 2})
        self.assertEqual(len(first.json()["data"]["items"]), 1)
        for query in ("嗜血", "Bloodthirst", "bloodthirst", "23881"):
            with self.subTest(query=query):
                rows = self.client.get("/api/simc-workbench/apl-symbols/", {"spec": "warrior_fury", "kind": "action", "query": query}).json()["data"]["items"]
                self.assertEqual([row["token"] for row in rows], ["bloodthirst"])
        rows = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury&kind=resource").json()["data"]["items"]
        self.assertEqual([row["token"] for row in rows], ["rage"])

    def test_catalog_identity_resolves_unique_full_revision_from_binary_version_suffix(self):
        revision = "62ababb127bef2a35f96357968d455dde7de7616"
        SimcBackendBinary.objects.create(
            platform="linux64", current_version="1205-01-62ababb")
        SimcAplSymbol.objects.create(
            simc_revision=revision, wow_build="12.0.7.68453",
            token="bloodthirst", symbol_kind="action", spell_id=23881)
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", snapshot_build="12.0.7.68453",
            spell_id=23881, name="Bloodthirst", name_zh="嗜血")

        response = self.client.get("/api/simc-workbench/apl-spells/", {
            "spec": "warrior_fury",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["items"][0]["token"], "bloodthirst")

    def test_spell_catalog_requires_current_identity_and_authoritative_symbol(self):
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=23881,
            name="Bloodthirst", name_zh="嗜血", snapshot_build="12.0.5",
        )

        response = self.client.get("/api/simc-workbench/apl-spells/?spec=warrior_fury")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "catalog_unavailable")

    @override_settings(SIMC_APL_CURRENT_IDENTITY=(REVISION, "12.0.5"))
    def test_spell_catalog_returns_only_current_spec_authoritative_actions(self):
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=23881,
            name="Blood Thirst!", name_zh="嗜血", snapshot_build="12.0.5",
        )
        SimcAplSymbol.objects.create(
            simc_revision=self.REVISION, wow_build="12.0.5", class_name="warrior",
            spec="fury", token="bloodthirst", symbol_kind="action", spell_id=23881,
        )
        SimcAplSymbol.objects.create(
            simc_revision=self.REVISION, wow_build="12.0.5", class_name="warrior",
            spec="arms", token="wrong_spec_token", symbol_kind="action", spell_id=23881,
        )

        item = self.client.get(
            "/api/simc-workbench/apl-spells/?spec=warrior_fury"
        ).json()["data"]["items"][0]

        self.assertEqual(item["token"], "bloodthirst")
        self.assertEqual(item["token_source"], "simc_symbol")
        self.assertIs(item["authoritative"], True)

    @override_settings(SIMC_APL_CURRENT_IDENTITY=(REVISION, "12.0.5"))
    def test_spell_catalog_excludes_other_specs_and_unbound_wago_rows(self):
        WowSpellSnapshot.objects.bulk_create([
            WowSpellSnapshot(
                branch="wow", locale="zhCN", spell_id=23881,
                name="Bloodthirst", name_zh="嗜血", snapshot_build="12.0.5"),
            WowSpellSnapshot(
                branch="wow", locale="zhCN", spell_id=12294,
                name="Mortal Strike", name_zh="致死打击", snapshot_build="12.0.5"),
            WowSpellSnapshot(
                branch="wow", locale="zhCN", spell_id=99999,
                name="Unrelated", name_zh="无关技能", snapshot_build="12.0.5"),
        ])
        SimcAplSymbol.objects.bulk_create([
            SimcAplSymbol(
                simc_revision=self.REVISION, wow_build="12.0.5", class_name="warrior",
                class_key="warrior", spec="fury", spec_key="fury",
                token="bloodthirst", symbol_kind="action", spell_id=23881),
            SimcAplSymbol(
                simc_revision=self.REVISION, wow_build="12.0.5", class_name="warrior",
                class_key="warrior", spec="arms", spec_key="arms",
                token="mortal_strike", symbol_kind="action", spell_id=12294),
        ])

        payload = self.client.get(
            "/api/simc-workbench/apl-spells/?spec=warrior_fury"
        ).json()["data"]

        self.assertEqual([row["token"] for row in payload["items"]], ["bloodthirst"])
        self.assertEqual(payload["pagination"]["total"], 1)
        self.assertTrue(payload["items"][0]["authoritative"])

    @override_settings(SIMC_APL_CURRENT_IDENTITY=(REVISION, "12.0.5"))
    def test_spell_catalog_searches_and_paginates_current_spec_in_database(self):
        fixtures = (
            (23881, "Bloodthirst", "嗜血", "bloodthirst"),
            (184367, "Rampage", "暴怒", "rampage"),
            (1719, "Recklessness", "鲁莽", "recklessness"),
        )
        WowSpellSnapshot.objects.bulk_create([
            WowSpellSnapshot(
                branch="wow", locale="zhCN", spell_id=spell_id,
                name=english, name_zh=chinese, snapshot_build="12.0.5")
            for spell_id, english, chinese, _token in fixtures
        ])
        SimcAplSymbol.objects.bulk_create([
            SimcAplSymbol(
                simc_revision=self.REVISION, wow_build="12.0.5", class_name="warrior",
                class_key="warrior", spec="fury", spec_key="fury",
                token=token, symbol_kind="action", spell_id=spell_id)
            for spell_id, _english, _chinese, token in fixtures
        ])

        for query in ("blood", "嗜血", "bloodthirst"):
            with self.subTest(query=query):
                with CaptureQueriesContext(connection) as captured:
                    response = self.client.get("/api/simc-workbench/apl-spells/", {
                        "spec": "warrior_fury", "query": query,
                        "page": 1, "page_size": 1,
                    })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"]["items"][0]["token"], "bloodthirst")
                self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
                catalog_sql = " ".join(
                    row["sql"] for row in captured.captured_queries
                    if "simc_apl_symbol" in row["sql"]
                ).upper()
                self.assertIn("LIMIT 1", catalog_sql)
                self.assertIn("SYMBOL_KIND", catalog_sql)
                self.assertIn("SPEC", catalog_sql)

    def test_completion_echoes_version_without_returning_document_or_querying_catalog(self):
        content = "actions.burst=/bloodthirst\nactions=/call_action_list,name=bu"
        with mock.patch("botend.dashboard.api.query_symbol_catalog") as catalog_query, \
                mock.patch("botend.dashboard.api._latest_catalog_identity") as identity_query:
            response = self.client.post("/api/simc-workbench/apl-completions/", data=json.dumps({
                "content": content, "position": {"line": 2, "column": 34}, "spec": "warrior_fury",
                "document_version": [9, "x"],
            }), content_type="application/json")
        catalog_query.assert_not_called()
        identity_query.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["document_version"], [9, "x"])
        self.assertIn("burst", [row["insert_text"] for row in response.json()["data"]["items"]])
        self.assertNotIn(content, response.content.decode())

    def test_completion_uses_document_semantics_for_line_starts_and_variables(self):
        content = "actions=/variable,name=pool,value=1\nactions+=/spell,if=variable.po"
        variable = self.client.post("/api/simc-workbench/apl-completions/", data=json.dumps({
            "content": content, "position": {"line": 2, "column": 31}, "spec": "warrior_fury",
        }), content_type="application/json").json()["data"]["items"]
        self.assertIn({"label": "pool", "insert_text": "pool", "kind": "variable"}, variable)
        line_start = self.client.post("/api/simc-workbench/apl-completions/", data=json.dumps({
            "content": "act", "position": {"line": 1, "column": 4}, "spec": "warrior_fury",
        }), content_type="application/json").json()["data"]["items"]
        self.assertIn("actions=", [item["insert_text"] for item in line_start])

    def test_validation_diagnostics_are_stably_paginated_and_page_size_is_bounded(self):
        content = "\n".join("invalid" for _ in range(205))
        response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({
            "content": content, "spec": "warrior_fury", "mode": "structural",
            "diagnostic_page": 2, "page_size": 10000,
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertNotIn("valid", data)
        self.assertEqual(data["pagination"], {
            "page": 2, "page_size": 100, "total": 205, "total_pages": 3,
        })
        self.assertEqual(len(data["diagnostics"]), 100)
        self.assertEqual(data["diagnostics"][0]["range"]["start"]["line"], 101)

    def test_validation_rejects_invalid_diagnostic_pagination(self):
        response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({
            "content": "actions=/auto_attack", "spec": "warrior_fury", "mode": "structural",
            "diagnostic_page": "not-a-number", "page_size": "also-not-a-number",
        }), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_pagination")

    def test_authoritative_modes_return_stable_structural_only_result_when_context_is_unavailable(self):
        for mode in ("authoritative", "both"):
            with self.subTest(mode=mode):
                response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({
                    "content": "actions=/auto_attack", "spec": "warrior_fury", "mode": mode,
                }), content_type="application/json")
                self.assertEqual(response.status_code, 200)
                data = response.json()["data"]
                self.assertEqual(data["authoritative_status"], "structural_only")
                self.assertEqual(data["authoritative_error"]["code"], "validation_context_unavailable")

    def test_symbol_items_include_catalog_identity_metadata(self):
        self._catalog()
        item = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury&page_size=1").json()["data"]["items"][0]
        self.assertEqual(item["simc_revision"], self.REVISION)
        self.assertEqual(item["game_build"], "11.2.0")

    @override_settings(SIMC_APL_EDITOR_RATE_LIMIT=1, SIMC_APL_EDITOR_RATE_WINDOW=60)
    def test_completion_and_symbols_frequency_are_limited(self):
        from botend.dashboard import api
        api._APL_EDITOR_RATE_BUCKETS.clear()
        SimcBackendBinary.objects.create(platform="linux64", current_version=self.REVISION)
        SimcAplSymbol.objects.create(simc_revision=self.REVISION, wow_build="current",
            class_name="warrior", spec="fury", token="current", symbol_kind="action")
        completion = json.dumps({"content": "act", "position": {"line": 1, "column": 4}, "spec": "warrior_fury"})
        self.assertEqual(self.client.post("/api/simc-workbench/apl-completions/", data=completion, content_type="application/json").status_code, 200)
        self.assertEqual(self.client.post("/api/simc-workbench/apl-completions/", data=completion, content_type="application/json").status_code, 429)
        self.assertEqual(self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury").status_code, 200)
        self.assertEqual(self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury").status_code, 429)

    def test_completion_and_symbol_queries_have_concurrency_boundaries(self):
        SimcBackendBinary.objects.create(platform="linux64", current_version=self.REVISION)
        SimcAplSymbol.objects.create(simc_revision=self.REVISION, wow_build="current",
            class_name="warrior", spec="fury", token="current", symbol_kind="action")
        completion = json.dumps({"content": "act", "position": {"line": 1, "column": 4}, "spec": "warrior_fury"})
        with mock.patch("botend.dashboard.api._APL_EDITOR_SEMAPHORE.acquire", return_value=False):
            completed = self.client.post("/api/simc-workbench/apl-completions/", data=completion, content_type="application/json")
            symbols = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury")
        self.assertEqual(completed.json()["error"]["code"], "concurrency_limited")
        self.assertEqual(symbols.json()["error"]["code"], "concurrency_limited")

    @override_settings(SIMC_APL_EDITOR_MAX_CONTENT_LENGTH=16)
    def test_large_content_is_rejected_without_echo_or_internal_output(self):
        secret = "/home/private/secret.apl STDERR: catastrophic-secret " + ("x" * 100)
        response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({"content": secret, "spec": "warrior_fury"}), content_type="application/json")
        self.assertEqual(response.status_code, 413)
        body = response.content.decode()
        self.assertNotIn(secret, body)
        self.assertNotIn("/home/private", body)
        self.assertNotIn("catastrophic-secret", body)

    @override_settings(SIMC_APL_EDITOR_RATE_LIMIT=1, SIMC_APL_EDITOR_RATE_WINDOW=60)
    def test_validation_frequency_is_limited(self):
        from botend.dashboard import api
        api._APL_EDITOR_RATE_BUCKETS.clear()
        payload = json.dumps({"content": "actions=/auto_attack", "spec": "warrior_fury"})
        self.assertEqual(self.client.post("/api/simc-workbench/apl-validation/", data=payload, content_type="application/json").status_code, 200)
        limited = self.client.post("/api/simc-workbench/apl-validation/", data=payload, content_type="application/json")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"]["code"], "rate_limited")

    def test_validation_concurrency_is_limited(self):
        with mock.patch("botend.dashboard.api._APL_EDITOR_SEMAPHORE.acquire", return_value=False):
            response = self.client.post("/api/simc-workbench/apl-validation/", data=json.dumps({"content": "actions=/auto_attack", "spec": "warrior_fury"}), content_type="application/json")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "concurrency_limited")
