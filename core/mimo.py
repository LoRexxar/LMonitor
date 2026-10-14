"""MiMo OpenAI Chat Completions adapter. Configuration/keys live in settings."""
from django.conf import settings as django_settings

from core.ai_errors import post_completion


class MiMoClient:
    def __init__(self, config=None):
        config = dict(getattr(django_settings, "MIMO_API_CONFIG", {}) or {}) if config is None else dict(config)
        self.enabled = config.get("enabled") is True
        self.api_key = str(config.get("api_key") or "").strip()
        self.base_url = str(config.get("base_url") or "").strip()
        self.model = str(config.get("model") or "").strip()
        self.request_timeout_seconds = float(config.get("request_timeout_seconds", 90))
        self.thinking_type = config.get("thinking_type", "disabled")
        if self.thinking_type not in ("enabled", "disabled"):
            raise ValueError("MiMo thinking_type must be enabled or disabled")
        if self.request_timeout_seconds <= 0:
            raise ValueError("MiMo request_timeout_seconds must be positive")

    @property
    def available(self):
        return bool(self.enabled and self.api_key and self.base_url and self.model)

    def create_completion(self, **kwargs):
        if not self.available:
            raise ValueError("MiMo fallback not enabled/configured")
        payload = dict(kwargs)
        payload["model"] = self.model
        limit = payload.pop("max_tokens", None)
        if limit is not None:
            existing = payload.get("max_completion_tokens")
            payload["max_completion_tokens"] = min(int(limit), int(existing)) if existing is not None else int(limit)
        if int(payload.get("max_completion_tokens", 0)) <= 0:
            raise ValueError("MiMo requires a positive output token limit")
        payload.setdefault("thinking", {"type": self.thinking_type})
        if payload["thinking"]["type"] == "enabled":
            payload.pop("temperature", None)
            payload.pop("top_p", None)
        return post_completion(provider="MiMo", base_url=self.base_url, api_key=self.api_key,
                               payload=payload, timeout=self.request_timeout_seconds)
