import json
import logging
import time

from core.ai_errors import error_details, is_transient_connection_error, post_completion
from core.mimo import MiMoClient
from core.codex import CodexClient

try:
    from zhipuai import ZhipuAI as ZhipuAiClient
except ImportError:
    try:
        from zai import ZhipuAiClient
    except ImportError:
        ZhipuAiClient = None


class GLMClient:
    def __init__(self, *, scope="general", provider_keys=None):
        if scope not in ("general", "translation"):
            raise ValueError("Unknown AI request scope")
        self.scope = scope
        self._provider_keys = frozenset(provider_keys) if provider_keys is not None else None
        self._in_call = False
        self.system_prompt = "你是一个冷酷无情的AI助手，你不需要展示思考过程，也不允许问我问题，只需要直接回答用户的问题和结果"
        self._load_config()
        self._reset_evidence()

    def _load_config(self):
        # Resolve one snapshot for this call/tool chain; administrative changes
        # take effect at the next public call, including on long-lived clients.
        from botend.services.ai_provider_service import resolve_ai_configs, _is_complete
        self._configs = resolve_ai_configs()
        config = self._configs["glm"]
        mimo_config = self._configs["mimo"]
        self._glm_enabled = config.get("enabled", True) is True
        self.api_key = str(config.get("api_key") or "").strip()
        self.base_url = str(config.get("base_url") or "").strip()
        self.coding_base_url = str(config.get("coding_base_url") or "").strip()
        self.max_tokens_text = int(config.get("max_tokens_text", 2400))
        self.max_tokens_tools = int(config.get("max_tokens_tools", 3600))
        self.request_timeout_seconds = float(config.get("request_timeout_seconds", 90))
        if self.request_timeout_seconds <= 0:
            raise ValueError("GLM request_timeout_seconds must be positive")
        configured = str(config.get("model") or "").strip()
        fallbacks = config.get("fallback_models", ["GLM-4.5-Flash", "GLM-4.5"]) or []
        self.model_candidates = list(dict.fromkeys(str(m).strip() for m in [configured] + list(fallbacks) if str(m).strip()))
        self.mimo = MiMoClient(mimo_config)
        self.codex = CodexClient(self._configs["codex"])
        self.primary_connection_attempts = max(1, min(3, int(mimo_config.get("primary_connection_attempts", 3))))
        self.primary_rate_limit_attempts = max(1, min(3, int(mimo_config.get("primary_rate_limit_attempts", 1))))
        self.primary_retry_delay_seconds = max(0.0, float(mimo_config.get("primary_retry_delay_seconds", 2)))
        self.client = self.coding_client = None
        # With a usable backup, use synchronous HTTP for GLM too. No abandoned
        # SDK worker can still run (or start hidden retries) after switching MiMo.
        if ZhipuAiClient and self.api_key and not self.mimo.available:
            preferred = self.coding_base_url or self.base_url
            options = {"api_key": self.api_key, "timeout": self.request_timeout_seconds, "max_retries": 0}
            try:
                self.client = ZhipuAiClient(**options, **({"base_url": preferred} if preferred else {}))
                if self.coding_base_url:
                    self.coding_client = ZhipuAiClient(**options, base_url=self.coding_base_url)
            except Exception:
                # An older SDK that rejects timeout/retry controls must not be
                # used unbounded; retain the configured HTTP path instead.
                self.client = self.coding_client = None
        ready = {
            "glm": bool(self._glm_enabled and _is_complete("glm", config) and self.model_candidates and
                        (self.client or self.coding_client or
                         (self.api_key and (self.base_url or self.coding_base_url)))),
            "mimo": self.mimo.available and _is_complete("mimo", mimo_config),
            "codex": self.codex.available and _is_complete("codex", self._configs["codex"]),
        }
        self._provider_order = sorted(
            (key for key, available in ready.items() if available
             and self.scope in self._configs[key]["_scopes"]
             and (self._provider_keys is None or key in self._provider_keys)),
            key=lambda key: (int(self._configs[key]["priority"]), key),
        )
        self._route_index = 0
        self._active_provider = self._provider_order[0] if self._provider_order else ""

    @property
    def available(self):
        if not self._in_call:
            self._load_config()
        return bool(self._provider_order)

    def _reset_evidence(self):
        self.last_error = ""
        self.last_reasoning = ""
        self.last_provider = ""
        self.last_model = ""
        self.last_primary_error = ""
        self._route_index = 0
        self._active_provider = self._provider_order[0] if self._provider_order else ""

    def _begin_call(self):
        self._reset_evidence()
        self._load_config()
        self._in_call = True

    def _safe_error(self, error):
        text = str(error)
        for key in (self.api_key, self.mimo.api_key, self.codex.api_key):
            if key:
                text = text.replace(key, "[redacted]")
        return text

    def _is_coding_model(self, model):
        m = str(model or "").strip().lower()
        return ("coding" in m) or m.startswith("code-") or m.startswith("codegeex")

    def _is_no_access_error(self, error):
        return error_details(error)[1] == "1220"

    def _is_rate_limit_error(self, error):
        status, code = error_details(error)
        if status in (400, 401, 403, 404, 422):
            return False
        return status == 429 or code in ("1302", "1308")

    def _failure_kind(self, error):
        status, _ = error_details(error)
        if status in (400, 401, 403, 404, 422):
            return "terminal"
        if self._is_rate_limit_error(error):
            return "rate_limit"
        if status is not None and 500 <= status <= 599:
            return "transient"
        return "transient" if is_transient_connection_error(error) else "terminal"

    def _openai_compat_endpoint(self, base_url):
        return f"{base_url.strip().rstrip('/')}/chat/completions" if base_url else ""

    def _create_completion_http(self, *, base_url, api_key, payload):
        if not base_url:
            raise ValueError("GLM HTTP base_url 未配置")
        return post_completion(provider="GLM", base_url=base_url, api_key=api_key,
                               payload=payload, timeout=self.request_timeout_seconds)

    def _record_attempt(self, provider_key, start, response=None, error=None):
        # Observability is best effort. A DB/status outage must never turn an
        # already successful paid completion into a retry or duplicate charge.
        try:
            from botend.services.ai_provider_service import record_ai_result
            record_ai_result(provider_key, self._configs[provider_key],
                             response=response, error=error, model=self.last_model,
                             latency_ms=round((time.monotonic() - start) * 1000))
        except Exception as failure:
            logging.getLogger(__name__).warning("AI status update failed: %s", type(failure).__name__)

    def _create_mimo_completion(self, **kwargs):
        self.last_provider = "mimo"
        self.last_model = self.mimo.model
        start = time.monotonic()
        try:
            response = self.mimo.create_completion(**kwargs)
        except Exception as error:
            self._record_attempt("mimo", start, error=error)
            raise
        self._record_attempt("mimo", start, response=response)
        return response

    def _create_codex_completion(self, **kwargs):
        self.last_provider = "codex"
        self.last_model = self.codex.model
        start = time.monotonic()
        try:
            response = self.codex.create_completion(**kwargs)
        except Exception as error:
            self._record_attempt("codex", start, error=error)
            raise
        self._record_attempt("codex", start, response=response)
        return response

    def _create_glm_attempt(self, client, base_url, req):
        start = time.monotonic()
        try:
            if client:
                response = client.chat.completions.create(**req)
            else:
                response = self._create_completion_http(base_url=base_url, api_key=self.api_key, payload=req)
        except Exception as error:
            self._record_attempt("glm", start, error=error)
            raise
        self._record_attempt("glm", start, response=response)
        return response

    def _request_with_retries(self, request):
        transient_failures = rate_failures = 0
        while True:
            try:
                return request()
            except Exception as error:
                # Retry only the initially selected provider. Backups get one
                # attempt; routing may advance to another in-scope provider,
                # but never cycles back to one already attempted.
                if self._route_index != 0:
                    raise
                kind = self._failure_kind(error)
                if kind == "rate_limit":
                    rate_failures += 1
                    retry = rate_failures < self.primary_rate_limit_attempts
                elif kind == "transient":
                    transient_failures += 1
                    retry = transient_failures < self.primary_connection_attempts
                else:
                    retry = False
                retry = retry and transient_failures + rate_failures < max(
                    self.primary_connection_attempts, self.primary_rate_limit_attempts)
                if not retry:
                    raise
                time.sleep(self.primary_retry_delay_seconds)

    def _create_glm_completion(self, **kwargs):
        for idx, model in enumerate(self.model_candidates):
            req = dict(kwargs, model=model)
            use_coding = self._is_coding_model(model)
            client = (self.coding_client if use_coding else self.client) or self.client or self.coding_client
            # Preserve the SDK's effective endpoint and billing source.
            base_url = self.coding_base_url or self.base_url
            self.last_provider, self.last_model = "glm", model
            try:
                return self._request_with_retries(lambda: self._create_glm_attempt(client, base_url, req))
            except Exception as error:
                if self._is_no_access_error(error) and idx < len(self.model_candidates) - 1:
                    continue
                raise

    def _create_completion(self, **kwargs):
        if not self.available:
            raise ValueError("没有已启用且配置完整的 AI 服务")
        while self._route_index < len(self._provider_order):
            provider_key = self._provider_order[self._route_index]
            self._active_provider = provider_key
            try:
                if provider_key == "glm":
                    return self._create_glm_completion(**kwargs)
                if provider_key == "mimo":
                    return self._request_with_retries(lambda: self._create_mimo_completion(**kwargs))
                if provider_key == "codex":
                    return self._request_with_retries(lambda: self._create_codex_completion(**kwargs))
                raise ValueError("Unknown AI provider")
            except Exception as error:
                if (self._failure_kind(error) not in ("rate_limit", "transient")
                        or self._route_index + 1 >= len(self._provider_order)):
                    raise
                if not self.last_primary_error:
                    self.last_primary_error = self._safe_error(error)
                self._route_index += 1

    def _normalize_message_content(self, msg):
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    value = item.get("text") or item.get("content")
                    if isinstance(value, str):
                        chunks.append(value.strip())
            return "\n".join(c for c in chunks if c).strip()
        return ""

    def _get_first_choice(self, response):
        if isinstance(response, dict):
            choices = response.get("choices") or []
            return choices[0] if choices else None
        try:
            return response.choices[0]
        except (AttributeError, IndexError, TypeError):
            return None

    def _get_choice_message(self, choice):
        return choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)

    def _get_choice_finish_reason(self, choice):
        return (choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", "")) or ""

    def _reasoning(self, msg):
        if isinstance(msg, dict):
            return msg.get("reasoning_content") or msg.get("reasoning") or ""
        return getattr(msg, "reasoning_content", "") or ""

    def send_message(self, message, max_tokens=None, thinking_type=None):
        try:
            self._begin_call()
            limit = int(self.max_tokens_text if max_tokens is None else max_tokens)
            if limit <= 0:
                raise ValueError("max_tokens must be positive")
            messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": message}]
            for attempt in range(3):
                req = {"messages": messages, "temperature": 0.7, "max_tokens": limit}
                if thinking_type in ("enabled", "disabled"):
                    req["thinking"] = {"type": thinking_type}
                response = self._create_completion(**req)
                choice = self._get_first_choice(response)
                msg = self._get_choice_message(choice)
                reasoning = self._reasoning(msg)
                if reasoning:
                    self.last_reasoning = str(reasoning)
                normalized = self._normalize_message_content(msg)
                if normalized:
                    self.last_error = ""
                    return normalized
                self.last_error = (f"empty content, finish_reason={self._get_choice_finish_reason(choice)}, "
                                   f"reasoning_len={len(str(reasoning))}")
                if attempt < 2:
                    time.sleep(1)
            return None
        except Exception as error:
            self.last_error = self._safe_error(error)
            return None
        finally:
            self._in_call = False

    def send_message_with_tools(self, message, tools, tool_handler, max_rounds=6):
        try:
            self._begin_call()
            if self.max_tokens_tools <= 0:
                raise ValueError("max_tokens_tools must be positive")
            messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": message}]
            executed = {}
            for _ in range(max_rounds):
                response = self._create_completion(messages=messages, tools=tools,
                                                   temperature=0.4, max_tokens=self.max_tokens_tools)
                choice = self._get_first_choice(response)
                msg = self._get_choice_message(choice)
                reasoning = self._reasoning(msg)
                if reasoning:
                    self.last_reasoning = str(reasoning)
                tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
                if not tool_calls:
                    result = self._normalize_message_content(msg)
                    if not result:
                        self.last_error = "empty content in tool conversation"
                        return None
                    self.last_error = ""
                    return result
                assistant_payload: dict = {"role": "assistant", "content": self._normalize_message_content(msg)}
                # MiMo thinking tools require the exact reasoning_content roundtrip.
                if isinstance(msg, dict) and "reasoning_content" in msg:
                    assistant_payload["reasoning_content"] = msg["reasoning_content"]
                elif reasoning:
                    assistant_payload["reasoning_content"] = reasoning
                assistant_tool_calls = []
                for tc in tool_calls:
                    func = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                    tool_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    name = func.get("name") if isinstance(func, dict) else getattr(func, "name", None)
                    raw = func.get("arguments", "{}") if isinstance(func, dict) else getattr(func, "arguments", "{}")
                    args = json.loads(raw)
                    if not tool_id or not name or not isinstance(args, dict):
                        raise ValueError("invalid tool call id/name/arguments")
                    assistant_tool_calls.append({"id": tool_id, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
                assistant_payload["tool_calls"] = assistant_tool_calls
                messages.append(assistant_payload)
                for tc in assistant_tool_calls:
                    name = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"])
                    signature = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                    if tc["id"] in executed:
                        previous_signature, result = executed[tc["id"]]
                        if signature != previous_signature:
                            raise ValueError("reused tool call id with different arguments")
                    else:
                        # Handler/business errors abort, not a reason to switch provider
                        # (and never re-run an already executed side-effecting handler).
                        result = tool_handler(name, args)
                        if not isinstance(result, str):
                            result = json.dumps(result, ensure_ascii=False)
                        executed[tc["id"]] = (signature, result)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            self.last_error = "tool conversation exceeded max_rounds"
            return None
        except Exception as error:
            self.last_error = self._safe_error(error)
            return None
        finally:
            self._in_call = False


def main():
    glm = GLMClient()
    print("GLM 客户端已初始化 (输入 'quit' 退出)")
    while True:
        user_input = input("您: ")
        if user_input.lower() == 'quit':
            break
        response = glm.send_message(user_input)
        if response:
            print(f"AI: {response}")
    print("再见！")


if __name__ == "__main__":
    main()
