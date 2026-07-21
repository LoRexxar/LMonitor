import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from botend.models import SimcAplKeywordPair, SimcAplSymbol, SimcProfile, SimcBackendBinary, WowSpellSnapshot


class SimcAplEditorApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="editor", password="password")
        self.client.force_login(self.user)

    def test_editor_language_api_converts_the_same_document_in_both_directions(self):
        SimcAplKeywordPair.objects.create(
            apl_keyword="bloodthirst", cn_keyword="嗜血", is_active=True)
        apl = "actions+=/bloodthirst,if=target.health.pct<20"

        chinese = self.client.post("/api/convert-text/", data=json.dumps({
            "text": apl, "conversion_type": "apl_to_cn",
        }), content_type="application/json")
        self.assertEqual(chinese.status_code, 200)
        self.assertEqual(chinese.json(), {
            "success": True,
            "result": "actions+=/嗜血,if=target.health.pct<20",
        })

        authoritative = self.client.post("/api/convert-text/", data=json.dumps({
            "text": chinese.json()["result"], "conversion_type": "cn_to_apl",
        }), content_type="application/json")
        self.assertEqual(authoritative.status_code, 200)
        self.assertEqual(authoritative.json(), {"success": True, "result": apl})

    def test_editor_language_api_treats_apl_underscores_and_spaces_as_equivalent(self):
        SimcAplKeywordPair.objects.create(
            apl_keyword="arcane_blast", cn_keyword="奥术冲击", is_active=True)

        for apl_variant in ("actions=/arcane_blast", "actions=/arcane blast"):
            with self.subTest(apl_variant=apl_variant):
                response = self.client.post("/api/convert-text/", data=json.dumps({
                    "text": apl_variant, "conversion_type": "apl_to_cn",
                }), content_type="application/json")
                self.assertEqual(response.json()["result"], "actions=/奥术冲击")

        for chinese_variant in ("actions=/奥术冲击", "actions=/奥术 冲击"):
            with self.subTest(chinese_variant=chinese_variant):
                response = self.client.post("/api/convert-text/", data=json.dumps({
                    "text": chinese_variant, "conversion_type": "cn_to_apl",
                }), content_type="application/json")
                self.assertEqual(response.json()["result"], "actions=/arcane_blast")

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
        SimcAplSymbol.objects.create(simc_revision="current", wow_build="current", class_name="warrior", spec="fury", token="current", symbol_kind="action")
        SimcBackendBinary.objects.create(platform="linux64", current_version="current")
        response = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury")
        self.assertEqual(response.json()["data"]["items"][0]["simc_revision"], "current")

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

    def test_symbols_reject_ambiguous_active_builds_for_current_revision(self):
        SimcBackendBinary.objects.create(platform="linux64", current_version="current")
        for wow_build in ("11.1.0", "11.2.0"):
            SimcAplSymbol.objects.create(simc_revision="current", wow_build=wow_build,
                class_name="warrior", spec="fury", token="bloodthirst", symbol_kind="action")

        response = self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"]["code"], "catalog_unavailable")

    def _catalog(self):
        for token, kind, spell_id in (("bloodthirst", "action", 23881), ("rampage", "action", 184367), ("rage", "resource", None)):
            SimcAplSymbol.objects.create(simc_revision="revision-secret-path", wow_build="11.2.0",
                class_name="warrior", spec="fury", token=token, symbol_kind=kind, spell_id=spell_id,
                source="simc_manifest")
        SimcBackendBinary.objects.create(platform="linux64", current_version="revision-secret-path")
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

    def test_wago_spell_catalog_uses_full_bilingual_snapshot_not_apl_keyword_pairs(self):
        WowSpellSnapshot.objects.create(
            locale="zhCN", spell_id=23881, name="Bloodthirst", name_zh="嗜血",
            snapshot_build="12.0.5",
        )
        SimcAplKeywordPair.objects.create(
            apl_keyword="legacy_only", cn_keyword="旧翻译表词条", is_active=True,
        )

        response = self.client.get("/api/simc-workbench/apl-spells/?spec=warrior_fury")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["items"], [{
            "english": "Bloodthirst", "chinese": "嗜血", "token": "bloodthirst",
            "token_source": "wago_candidate", "authoritative": False,
        }])
        self.assertNotIn("legacy_only", response.content.decode())

    @override_settings(SIMC_APL_CURRENT_IDENTITY=("current", "12.0.5"))
    def test_wago_spell_catalog_prefers_current_spec_symbol_by_spell_id(self):
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=23881,
            name="Blood Thirst!", name_zh="嗜血", snapshot_build="12.0.5",
        )
        SimcAplSymbol.objects.create(
            simc_revision="current", wow_build="12.0.5", class_name="warrior",
            spec="fury", token="bloodthirst", symbol_kind="action", spell_id=23881,
        )
        SimcAplSymbol.objects.create(
            simc_revision="current", wow_build="12.0.5", class_name="warrior",
            spec="arms", token="wrong_spec_token", symbol_kind="action", spell_id=23881,
        )

        item = self.client.get(
            "/api/simc-workbench/apl-spells/?spec=warrior_fury"
        ).json()["data"]["items"][0]

        self.assertEqual(item["token"], "bloodthirst")
        self.assertEqual(item["token_source"], "simc_symbol")
        self.assertIs(item["authoritative"], True)

    @override_settings(SIMC_APL_CURRENT_IDENTITY=("current", "12.0.5"))
    def test_wago_spell_catalog_does_not_guess_between_conflicting_spell_id_symbols(self):
        for spell_id in (23881, 23882):
            WowSpellSnapshot.objects.create(
                branch="wow", locale="zhCN", spell_id=spell_id,
                name="Shared Name", name_zh="共享名称", snapshot_build="12.0.5",
            )
        for spell_id, token in ((23881, "first_action"), (23882, "second_action")):
            SimcAplSymbol.objects.create(
                simc_revision="current", wow_build="12.0.5", class_name="warrior",
                spec="fury", token=token, symbol_kind="action", spell_id=spell_id,
            )

        item = self.client.get(
            "/api/simc-workbench/apl-spells/?spec=warrior_fury"
        ).json()["data"]["items"][0]

        self.assertEqual(item["token"], "shared_name")
        self.assertEqual(item["token_source"], "wago_candidate")
        self.assertIs(item["authoritative"], False)

    def test_wago_spell_catalog_uses_keyword_pair_only_as_token_mapping(self):
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=30451,
            name="Arcane Blast", name_zh="奥术冲击", snapshot_build="12.0.5",
        )
        SimcAplKeywordPair.objects.create(
            apl_keyword="arcane_blast", cn_keyword="奥术冲击", is_active=True,
        )

        payload = self.client.get(
            "/api/simc-workbench/apl-spells/?spec=mage_arcane"
        ).json()["data"]

        self.assertEqual(payload["pagination"]["total"], 1)
        self.assertEqual(payload["items"][0]["token"], "arcane_blast")
        self.assertEqual(payload["items"][0]["token_source"], "keyword_pair")
        self.assertIs(payload["items"][0]["authoritative"], False)

    def test_wago_spell_catalog_keyword_mapping_requires_exact_english_and_chinese_pair(self):
        WowSpellSnapshot.objects.create(
            branch="wow", locale="zhCN", spell_id=90001,
            name="Unrelated Blast", name_zh="奥术冲击", snapshot_build="12.0.5",
        )
        SimcAplKeywordPair.objects.create(
            apl_keyword="arcane_blast", cn_keyword="奥术冲击", is_active=True,
        )

        item = self.client.get(
            "/api/simc-workbench/apl-spells/?spec=mage_arcane"
        ).json()["data"]["items"][0]

        self.assertEqual(item["token"], "unrelated_blast")
        self.assertEqual(item["token_source"], "wago_candidate")
        self.assertIs(item["authoritative"], False)

    def test_wago_spell_catalog_searches_both_languages_and_paginates(self):
        for spell_id, english, chinese in (
                (23881, "Bloodthirst", "嗜血"),
                (184367, "Rampage", "暴怒"),
                (1719, "Recklessness", "鲁莽")):
            WowSpellSnapshot.objects.create(
                locale="zhCN", spell_id=spell_id, name=english, name_zh=chinese,
                snapshot_build="12.0.5",
            )

        for query in ("blood", "嗜血"):
            response = self.client.get("/api/simc-workbench/apl-spells/", {
                "query": query, "page": 1, "page_size": 1,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["items"][0]["english"], "Bloodthirst")
            self.assertEqual(response.json()["data"]["pagination"]["total"], 1)

    def test_wago_spell_catalog_only_uses_live_branch_and_deduplicates_names(self):
        for spell_id, branch in ((23881, "wow"), (23882, "wow"), (23883, "wowt")):
            WowSpellSnapshot.objects.create(
                branch=branch, locale="zhCN", spell_id=spell_id,
                name="Bloodthirst", name_zh="嗜血", snapshot_build="12.0.5",
            )

        response = self.client.get("/api/simc-workbench/apl-spells/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(response.json()["data"]["items"], [{
            "english": "Bloodthirst", "chinese": "嗜血", "token": "bloodthirst",
            "token_source": "wago_candidate", "authoritative": False,
        }])

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
        self.assertEqual(item["simc_revision"], "revision-secret-path")
        self.assertEqual(item["game_build"], "11.2.0")

    @override_settings(SIMC_APL_EDITOR_RATE_LIMIT=1, SIMC_APL_EDITOR_RATE_WINDOW=60)
    def test_completion_and_symbols_frequency_are_limited(self):
        from botend.dashboard import api
        api._APL_EDITOR_RATE_BUCKETS.clear()
        SimcBackendBinary.objects.create(platform="linux64", current_version="current")
        SimcAplSymbol.objects.create(simc_revision="current", wow_build="current",
            class_name="warrior", spec="fury", token="current", symbol_kind="action")
        completion = json.dumps({"content": "act", "position": {"line": 1, "column": 4}, "spec": "warrior_fury"})
        self.assertEqual(self.client.post("/api/simc-workbench/apl-completions/", data=completion, content_type="application/json").status_code, 200)
        self.assertEqual(self.client.post("/api/simc-workbench/apl-completions/", data=completion, content_type="application/json").status_code, 429)
        self.assertEqual(self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury").status_code, 200)
        self.assertEqual(self.client.get("/api/simc-workbench/apl-symbols/?spec=warrior_fury").status_code, 429)

    def test_completion_and_symbol_queries_have_concurrency_boundaries(self):
        SimcBackendBinary.objects.create(platform="linux64", current_version="current")
        SimcAplSymbol.objects.create(simc_revision="current", wow_build="current",
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
