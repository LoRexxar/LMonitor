"""Existing Codex translation endpoint, with bounded OpenAI-compatible HTTP."""
from urllib.parse import urljoin

from core.ai_errors import post_completion


class CodexClient:
    def __init__(self, config):
        self.enabled = config.get('enabled', True) is True
        self.api_key = str(config.get('api_key') or '').strip()
        self.base_url = str(config.get('base_url') or '').strip()
        self.model = str(config.get('model') or '').strip()
        self.request_timeout_seconds = float(config.get('request_timeout_seconds', 90))

    @property
    def available(self):
        return bool(self.enabled and self.api_key and self.base_url and self.model)

    def create_completion(self, **kwargs):
        if not self.available:
            raise ValueError('Codex translation AI not enabled/configured')
        payload = dict(kwargs)
        payload['model'] = self.model
        payload.pop('thinking', None)
        # Preserve the existing translator's temperature and system instruction.
        payload['temperature'] = 0.2
        messages = list(payload.get('messages') or [])
        if messages and messages[0].get('role') == 'system':
            messages[0] = {**messages[0], 'content': '你是专业的游戏资讯翻译。只输出用户要求的翻译结果，不要添加解释。'}
        payload['messages'] = messages
        # Match the existing urljoin(base + '/', 'v1/chat/completions') route;
        # enabling management must not silently repair/relocate opaque endpoints.
        base = urljoin(self.base_url.rstrip('/') + '/', 'v1').rstrip('/')
        return post_completion(provider='Codex', base_url=base, api_key=self.api_key,
                               payload=payload, timeout=self.request_timeout_seconds)
