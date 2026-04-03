from zai import ZhipuAiClient
import os
import json
import concurrent.futures
import time
from LMonitor.settings import ZHIYU_API_CONFIG

class GLMClient:
    def __init__(self):
        self.client = ZhipuAiClient(api_key=ZHIYU_API_CONFIG["api_key"])
        self.system_prompt = "你是一个冷酷无情的AI助手，你不需要展示思考过程，也不允许问我问题，只需要直接回答用户的问题和结果"
        self.max_tokens_text = int(ZHIYU_API_CONFIG.get("max_tokens_text", 2400))
        self.max_tokens_tools = int(ZHIYU_API_CONFIG.get("max_tokens_tools", 3600))
        self.request_timeout_seconds = int(ZHIYU_API_CONFIG.get("request_timeout_seconds", 90))
        configured = str(ZHIYU_API_CONFIG.get("model", "")).strip()
        fallbacks = ZHIYU_API_CONFIG.get("fallback_models", []) or ["GLM-4.5-Flash", "GLM-4.5"]
        self.model_candidates = [m for m in [configured] + list(fallbacks) if str(m).strip()]
        self.last_error = ""

    def _is_no_access_error(self, e):
        msg = str(e or '')
        return ('"code":"1220"' in msg) or ('无权访问' in msg) or ('no permission' in msg.lower())

    def _is_rate_limit_error(self, e):
        msg = str(e or '')
        return ('"code":"1302"' in msg) or ('速率限制' in msg) or ('rate limit' in msg.lower()) or ('429' in msg)

    def _create_completion(self, **kwargs):
        last = None
        for idx, model in enumerate(self.model_candidates):
            req = dict(kwargs)
            req["model"] = model
            for retry in range(3):
                try:
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(self.client.chat.completions.create, **req)
                    try:
                        return future.result(timeout=self.request_timeout_seconds)
                    except concurrent.futures.TimeoutError:
                        raise Exception(f"GLM请求超时（>{self.request_timeout_seconds}s）")
                    finally:
                        executor.shutdown(wait=False, cancel_futures=True)
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

    def send_message(self, message):
        self.last_error = ""
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": message}
            ]
            response = self._create_completion(
                messages=messages,
                temperature=0.7,
                max_tokens=self.max_tokens_text
            )
            return response.choices[0].message.content
        except Exception as e:
            self.last_error = str(e)
            print(f"发生错误: {e}")
            return None

    def send_message_with_tools(self, message, tools, tool_handler, max_rounds=6):
        self.last_error = ""
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
                msg = response.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None)
                if not tool_calls:
                    return getattr(msg, "content", None)

                assistant_payload = {"role": "assistant", "content": getattr(msg, "content", "")}
                assistant_tool_calls = []
                for tc in tool_calls:
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
            print(f"发生错误: {e}")
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
