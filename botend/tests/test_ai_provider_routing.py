"""Actual HTTP priority/enablement contracts for the managed AI router."""
import unittest
from django.test import override_settings
from botend.tests.test_glm_mimo_fallback import GLMMiMoHTTPContracts,answer,tool_answer


class AIProviderRoutingContracts(unittest.TestCase):
    setUp=GLMMiMoHTTPContracts.setUp
    client=GLMMiMoHTTPContracts.client

    def test_codex_priority_is_translation_only_and_uses_legacy_endpoint(self):
        from django.conf import settings
        from core.glm import GLMClient
        codex={'enabled':True,'priority':0,'api_key':'codex-fixture-key','model':'codex-fixture',
               'base_url':self.backup.url.removesuffix('/v1'),'request_timeout_seconds':0.2}
        with override_settings(CODEX_API_CONFIG=codex):
            general=GLMClient()
            self.assertEqual(general.send_message('ordinary'),'ok')
            self.assertEqual(general.last_provider,'glm')
            self.assertFalse(self.backup.requests)
            translation=GLMClient(scope='translation')
            self.backup.actions.append((200,answer('translated')))
            self.assertEqual(translation.send_message('translate',max_tokens=100,thinking_type='disabled'),'translated')
            self.assertEqual(translation.last_provider,'codex')
            request=self.backup.requests[-1]
            self.assertEqual(request['path'],'/v1/chat/completions')
            self.assertNotIn('thinking',request['payload'])
            self.assertEqual(request['payload']['max_tokens'],100)
            self.assertEqual(request['payload']['temperature'],0.2)
            self.assertEqual(len(self.primary.requests),1)

    def test_availability_updates_after_enable_without_a_prior_send(self):
        from core.glm import GLMClient
        with override_settings(ZHIYU_API_CONFIG={**self.config,'enabled':False},MIMO_API_CONFIG={**self.mimo,'enabled':False}):
            client=GLMClient()
            self.assertFalse(client.available)
            with override_settings(MIMO_API_CONFIG={**self.mimo,'enabled':True}):
                self.assertTrue(client.available)
                self.assertEqual(client.send_message('newly enabled'),'ok')
                self.assertEqual(client.last_provider,'mimo')

    def test_default_translation_service_uses_central_provider_order(self):
        from botend.services.article_translation_service import FallbackTranslationEngine
        codex={'enabled':True,'priority':0,'api_key':'codex-fixture-key','model':'codex-fixture',
               'base_url':self.backup.url.removesuffix('/v1'),'request_timeout_seconds':0.2}
        with override_settings(CODEX_API_CONFIG=codex):
            self.backup.actions.append((200,answer('translation pipeline')))
            engine=FallbackTranslationEngine()
            self.assertEqual(engine.send_message('translate',max_tokens=200),'translation pipeline')
            self.assertFalse(self.primary.requests)
            self.assertEqual(len(self.backup.requests),1)

    def test_priority_selects_mimo_first_without_calling_glm(self):
        self.backup.actions.append((200,answer('mimo first')))
        with override_settings(MIMO_API_CONFIG={**self.mimo,'priority':0}):
            client=self.client()
            self.assertEqual(client.send_message('priority'),'mimo first')
            self.assertEqual(client.last_provider,'mimo')
            self.assertFalse(self.primary.requests)

    def test_disabled_glm_is_not_called_and_disabled_all_is_unavailable(self):
        with override_settings(ZHIYU_API_CONFIG={**self.config,'enabled':False}):
            client=self.client()
            self.assertTrue(client.available)
            self.assertEqual(client.send_message('only mimo'),'ok')
            self.assertEqual(client.last_provider,'mimo')
            self.assertFalse(self.primary.requests)
        before=len(self.backup.requests)
        with override_settings(ZHIYU_API_CONFIG={**self.config,'enabled':False},MIMO_API_CONFIG={**self.mimo,'enabled':False}):
            client=self.client()
            self.assertFalse(client.available)
            self.assertIsNone(client.send_message('none'))
            self.assertEqual(len(self.backup.requests),before)

    def test_priority_refreshes_on_next_call_but_not_mid_tool_chain(self):
        client=self.client()
        self.backup.actions.extend([(200,tool_answer()),(200,answer('pinned mimo'))])
        with override_settings(MIMO_API_CONFIG={**self.mimo,'priority':0}):
            def handler(name,args):
                from django.conf import settings
                settings.MIMO_API_CONFIG={**self.mimo,'priority':20}
                return {'value':99}
            self.assertEqual(client.send_message_with_tools('tools',[],handler),'pinned mimo')
            self.assertEqual(client.last_provider,'mimo')
            self.assertFalse(self.primary.requests)
            self.primary.actions.append((200,answer('next glm')))
            self.assertEqual(client.send_message('next'),'next glm')
            self.assertEqual(client.last_provider,'glm')

    def test_mimo_primary_connection_failures_can_fall_back_to_glm_once(self):
        self.backup.actions.extend(['disconnect']*3)
        self.primary.actions.append((200,answer('glm backup')))
        with override_settings(MIMO_API_CONFIG={**self.mimo,'priority':0}):
            client=self.client()
            self.assertEqual(client.send_message('reverse fallback'),'glm backup')
            self.assertEqual(client.last_provider,'glm')
            self.assertEqual((len(self.backup.requests),len(self.primary.requests)),(3,1))
