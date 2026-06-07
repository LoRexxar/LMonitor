import os
import json
import concurrent.futures
import time
from urllib.parse import urljoin

import requests
from LMonitor.settings import ZHIYU_API_CONFIG

try:
    from zai import ZhipuAiClient
except Exception:
    ZhipuAiClient = None

class GLMClient:
    def __init__(self):
        self.client = None
        self.coding_client = None
        self.api_key = (ZHIYU_API_CONFIG.get("api_key") or "").strip()
        self.base_url = (ZHIYU_API_CONFIG.get("base_url") or "").strip()
        self.coding_base_url = (ZHIYU_API_CONFIG.get("coding_base_url") or "").strip()

        # 优先使用 SDK（如果存在）；否则走 OpenAI 兼容 HTTP 接口兜底（无需额外依赖）。
        if ZhipuAiClient and self.api_key:
            preferred_base_url = self.coding_base_url or self.base_url
            try:
                if preferred_base_url:
                    self.client = ZhipuAiClient(api_key=self.api_key, base_url=preferred_base_url)
                else:
                    self.client = ZhipuAiClient(api_key=self.api_key)
            except Exception:
                self.client = None
            try:
                if self.coding_base_url:
                    self.coding_client = ZhipuAiClient(api_key=self.api_key, base_url=self.coding_base_url)
            except Exception:
                self.coding_client = None
        self.system_prompt = "你是一个冷酷无情的AI助手，你不需要展示思考过程，也不允许问我问题，只需要直接回答用户的问题和结果"
        self.max_tokens_text = int(ZHIYU_API_CONFIG.get("max_tokens_text", 2400))
        self.max_tokens_tools = int(ZHIYU_API_CONFIG.get("max_tokens_tools", 3600))
        self.request_timeout_seconds = int(ZHIYU_API_CONFIG.get("request_timeout_seconds", 90))
        configured = str(ZHIYU_API_CONFIG.get("model", "")).strip()
        fallbacks = ZHIYU_API_CONFIG.get("fallback_models", []) or ["GLM-4.5-Flash", "GLM-4.5"]
        self.model_candidates = [m for m in [configured] + list(fallbacks) if str(m).strip()]
        self.last_error = ""
        self.last_reasoning = ""

    def _is_coding_model(self, model):
        m = str(model or "").strip().lower()
        return ("coding" in m) or m.startswith("code-") or m.startswith("codegeex")

    def _is_no_access_error(self, e):
        msg = str(e or '')
        return ('"code":"1220"' in msg) or ('无权访问' in msg) or ('no permission' in msg.lower())

    def _is_rate_limit_error(self, e):
        msg = str(e or '')
        return ('"code":"1302"' in msg) or ('速率限制' in msg) or ('rate limit' in msg.lower()) or ('429' in msg)

    def _openai_compat_endpoint(self, base_url: str):
        b = (base_url or "").strip().rstrip("/")
        if not b:
            return ""
        # OpenAI 兼容接口：.../chat/completions
        return f"{b}/chat/completions"

    def _create_completion_http(self, *, base_url: str, api_key: str, payload: dict):
        url = self._openai_compat_endpoint(base_url)
        if not url:
            raise Exception("GLM HTTP base_url 未配置")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.request_timeout_seconds)
        except Exception as e:
            raise Exception(f"GLM HTTP 请求失败: {e}")
        if resp.status_code >= 400:
            txt = (resp.text or "").strip()
            raise Exception(f"GLM HTTP bad status={resp.status_code}: {txt[:800]}")
        try:
            return resp.json()
        except Exception:
            raise Exception(f"GLM HTTP 返回非 JSON: {(resp.text or '')[:800]}")

    def _create_completion(self, **kwargs):
        if not self.client and not self.coding_client:
            # 走 HTTP 兼容接口兜底（无需 SDK）
            if not (self.api_key and (self.base_url or self.coding_base_url)):
                raise Exception("GLM SDK未安装/未初始化，且未配置 HTTP base_url/api_key")
        last = None
        for idx, model in enumerate(self.model_candidates):
            req = dict(kwargs)
            req["model"] = model
            use_coding = self._is_coding_model(model)
            client = self.coding_client if use_coding else self.client
            if not client:
                client = self.client or self.coding_client
            for retry in range(3):
                try:
                    # SDK 优先
                    if client:
                        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                        future = executor.submit(client.chat.completions.create, **req)
                        try:
                            return future.result(timeout=self.request_timeout_seconds)
                        except concurrent.futures.TimeoutError:
                            raise Exception(f"GLM请求超时（>{self.request_timeout_seconds}s）")
                        finally:
                            executor.shutdown(wait=False, cancel_futures=True)
                    # HTTP 兜底
                    base_url = self.coding_base_url if use_coding and self.coding_base_url else self.base_url
                    return self._create_completion_http(base_url=base_url, api_key=self.api_key, payload=req)
                except Exception as e:
                    last = e
                    if self._is_rate_limit_error(e) and retry < 2:
                        time.sleep(2 * (retry + 1))
                        continue
                    if self._is_no_access_error(e) and idx < len(self.model_candidates) - 1:
                        break
                    raise
        if last:
            raise last

    def _normalize_message_content(self, msg):
        if isinstance(msg, dict):
            content = msg.get("content", None)
        else:
            content = getattr(msg, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, str):
                    chunks.append(item)
                    continue
                if isinstance(item, dict):
                    text_val = item.get("text")
                    if isinstance(text_val, str) and text_val.strip():
                        chunks.append(text_val.strip())
                        continue
                    # 兼容部分SDK把文本放在 content 字段
                    content_val = item.get("content")
                    if isinstance(content_val, str) and content_val.strip():
                        chunks.append(content_val.strip())
            return "\n".join([c for c in chunks if c]).strip()
        return ""

    def _get_first_choice(self, response):
        if isinstance(response, dict):
            choices = response.get("choices") or []
            return choices[0] if choices else None
        try:
            return response.choices[0]
        except Exception:
            return None

    def _get_choice_message(self, choice):
        if not choice:
            return None
        if isinstance(choice, dict):
            return choice.get("message")
        return getattr(choice, "message", None)

    def _get_choice_finish_reason(self, choice):
        if not choice:
            return ""
        if isinstance(choice, dict):
            return choice.get("finish_reason") or ""
        return getattr(choice, "finish_reason", "") or ""

    def send_message(self, message, max_tokens=None, thinking_type=None):
        self.last_error = ""
        self.last_reasoning = ""
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": message}
            ]
            # 兼容“请求成功但content为空”的场景，做有限重试
            for _ in range(3):
                req = {
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": int(max_tokens or self.max_tokens_text)
                }
                if thinking_type in ("enabled", "disabled"):
                    req["thinking"] = {"type": thinking_type}
                response = self._create_completion(**req)
                choice = self._get_first_choice(response)
                msg = self._get_choice_message(choice)
                if isinstance(msg, dict):
                    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "") or ""
                else:
                    reasoning = getattr(msg, "reasoning_content", "") or ""
                if reasoning:
                    self.last_reasoning = str(reasoning)
                normalized = self._normalize_message_content(msg)
                if normalized:
                    return normalized
                finish_reason = self._get_choice_finish_reason(choice)
                self.last_error = f"empty content, finish_reason={finish_reason}, reasoning_len={len(str(reasoning))}"
                time.sleep(1)
            return None
        except Exception as e:
            self.last_error = str(e)
            return None

    def send_message_with_tools(self, message, tools, tool_handler, max_rounds=6):
        self.last_error = ""
        self.last_reasoning = ""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": message}
        ]
        try:
            for _ in range(max_rounds):
                response = self._create_completion(
                    messages=messages,
                    tools=tools,
                    temperature=0.4,
                    max_tokens=self.max_tokens_tools
                )
                choice = self._get_first_choice(response)
                msg = self._get_choice_message(choice)
                if isinstance(msg, dict):
                    tool_calls = msg.get("tool_calls", None)
                else:
                    tool_calls = getattr(msg, "tool_calls", None)
                if not tool_calls:
                    if isinstance(msg, dict):
                        return msg.get("content", None)
                    return getattr(msg, "content", None)

                assistant_payload = {
                    "role": "assistant",
                    "content": (msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")) or ""
                }
                assistant_tool_calls = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        func = tc.get("function") or {}
                        tool_id = tc.get("id", None)
                        function_name = (func.get("name") or "").strip()
                        arguments_raw = func.get("arguments", "{}") or "{}"
                    else:
                        func = getattr(tc, "function", None)
                        if not func:
                            continue
                        tool_id = getattr(tc, "id", None)
                        function_name = getattr(func, "name", "")
                        arguments_raw = getattr(func, "arguments", "{}") or "{}"
                    try:
                        arguments = json.loads(arguments_raw)
                    except Exception:
                        arguments = {}
                    assistant_tool_calls.append({
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": function_name,
                            "arguments": json.dumps(arguments, ensure_ascii=False)
                        }
                    })
                assistant_payload["tool_calls"] = assistant_tool_calls
                messages.append(assistant_payload)

                for tc in assistant_tool_calls:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        func_args = {}
                    try:
                        tool_result = tool_handler(func_name, func_args)
                        if not isinstance(tool_result, str):
                            tool_result = json.dumps(tool_result, ensure_ascii=False)
                    except Exception as e:
                        tool_result = json.dumps({"error": str(e)}, ensure_ascii=False)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result
                    })
            return None
        except Exception as e:
            self.last_error = str(e)
            return None

def main():
    # 示例用法
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
