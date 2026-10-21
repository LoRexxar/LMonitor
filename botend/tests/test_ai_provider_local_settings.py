"""验证本地模型配置不会读取或写入共享数据库。"""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from botend.services import ai_provider_service as service


@override_settings(
    AI_PROVIDER_USE_DATABASE=False,
    ZHIYU_API_CONFIG={'enabled': False},
    CODEX_API_CONFIG={'enabled': False},
    MIMO_API_CONFIG={'enabled': True, 'priority': 0, 'model': 'mimo-fixture',
                     'api_key': 'fixture-local-key', 'base_url': 'https://mimo.example/v1'},
)
class LocalAIProviderSettingsTests(SimpleTestCase):
    def test_local_resolution_does_not_access_configuration_table(self):
        with patch.object(service, '_configuration_model', side_effect=AssertionError('不应访问数据库')):
            configs = service.resolve_ai_configs()
        self.assertTrue(configs['mimo']['enabled'])
        self.assertEqual(configs['mimo']['model'], 'mimo-fixture')
        self.assertFalse(configs['glm']['enabled'])

    def test_local_results_do_not_write_shared_provider_state(self):
        with patch.object(service, '_configuration_model', side_effect=AssertionError('不应访问数据库')) as model:
            service.record_ai_result('mimo', {}, response={'choices': []})
        model.assert_not_called()

    @override_settings(AI_PROVIDER_USE_DATABASE=True)
    def test_database_mode_still_reads_configuration_rows(self):
        with patch.object(service, '_configuration_model') as model:
            model.return_value.objects.filter.return_value = []
            service.resolve_ai_configs()
        model.return_value.objects.filter.assert_called_once()
