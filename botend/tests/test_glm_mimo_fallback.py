"""Real loopback HTTP contracts; no provider SDK/network mocks or live keys."""
import json
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.conf import settings
from django.test import override_settings

if not settings.configured:
    settings.configure(SECRET_KEY="loopback-tests-only", ZHIYU_API_CONFIG={})

from core.glm import GLMClient


def answer(content="ok", **fields):
    return {"choices": [{"message": {"role": "assistant", "content": content, **fields},
                         "finish_reason": "stop"}]}


def tool_answer(tool_id="lookup-1", reasoning="analysis"):
    return answer("", reasoning_content=reasoning, tool_calls=[{
        "id": tool_id, "type": "function",
        "function": {"name": "lookup", "arguments": '{"id": 42}'},
    }])


class HTTPFixture:
    def __init__(self, tls=False):
        self.actions = deque()
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                fixture.requests.append({"path": self.path, "headers": dict(self.headers),
                                         "payload": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                action = fixture.actions.popleft() if fixture.actions else (200, answer())
                if action == "disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if action == "timeout":
                    time.sleep(0.15)
                    action = (200, answer("late"))
                status, body = action
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.cert_dir = None
        if tls:
            self.cert_dir = tempfile.TemporaryDirectory()
            cert = self.cert_dir.name + "/cert.pem"
            key = self.cert_dir.name + "/key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-keyout", key, "-out", cert],
                           check=True, capture_output=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.cert_dir is not None:
            self.cert_dir.cleanup()


class GLMMiMoHTTPContracts(unittest.TestCase):
    def setUp(self):
        self.primary = HTTPFixture()
        self.backup = HTTPFixture()
        self.addCleanup(self.primary.close)
        self.addCleanup(self.backup.close)
        self.config = {
            "api_key": "fixture-glm-key", "base_url": self.primary.url,
            "model": "glm-fixture", "fallback_models": [],
            "request_timeout_seconds": 0.04,
        }
        self.mimo = {
            "enabled": True, "api_key": "fixture-mimo-key", "base_url": self.backup.url,
            "model": "mimo-v2.6-flash", "request_timeout_seconds": 0.04,
            "thinking_type": "disabled", "primary_connection_attempts": 3,
            "primary_rate_limit_attempts": 1, "primary_retry_delay_seconds": 0,
        }
        self.override = override_settings(ZHIYU_API_CONFIG=self.config, MIMO_API_CONFIG=self.mimo)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def client(self):
        return GLMClient()

    def test_rate_limit_switches_and_new_call_prefers_glm(self):
        self.primary.actions.extend([(429, {"error": {"code": "1302"}}), (200, answer("primary"))])
        self.backup.actions.append((200, answer("backup", reasoning_content="backup reasoning")))
        client = self.client()
        self.assertEqual(client.send_message("hello", max_tokens=123, thinking_type="disabled"), "backup")
        self.assertTrue(client.available)
        self.assertTrue(client.mimo.available)
        self.assertEqual((client.last_provider, client.last_model), ("mimo", "mimo-v2.6-flash"))
        self.assertIn("429", client.last_primary_error)
        self.assertEqual(client.last_error, "")
        self.assertEqual(client.last_reasoning, "backup reasoning")
        request = self.backup.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer fixture-mimo-key")
        self.assertEqual(request["payload"]["model"], "mimo-v2.6-flash")
        self.assertEqual(request["payload"]["max_completion_tokens"], 123)
        self.assertNotIn("max_tokens", request["payload"])
        self.assertEqual(request["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(client.send_message("again"), "primary")
        self.assertEqual((client.last_provider, client.last_model, client.last_primary_error),
                         ("glm", "glm-fixture", ""))
        self.assertEqual(len(self.primary.requests), 2)

    def test_structured_quota_codes_switch_without_string_matching(self):
        for code in (1302, "1308"):
            with self.subTest(code=code):
                self.primary.actions.append((200, {"error": {"code": code, "message": "quota"}}))
                self.backup.actions.append((200, answer("backup")))
                self.assertEqual(self.client().send_message("hello"), "backup")

    def test_connection_timeout_and_server_errors_are_bounded(self):
        for failure in ("disconnect", "timeout", (503, {"error": {"message": "down"}})):
            with self.subTest(failure=failure):
                before = len(self.primary.requests)
                self.primary.actions.extend([failure] * 3)
                self.backup.actions.append((200, answer("backup")))
                self.assertEqual(self.client().send_message("hello"), "backup")
                self.assertEqual(len(self.primary.requests) - before, 3)

    def test_transient_primary_recovers_without_switch(self):
        self.primary.actions.extend(["disconnect", (200, answer("recovered"))])
        client = self.client()
        self.assertEqual(client.send_message("hello"), "recovered")
        self.assertEqual(client.last_provider, "glm")
        self.assertFalse(self.backup.requests)

    def test_bad_requests_denials_json_and_fake_429_do_not_switch(self):
        actions: list = [(code, {"error": {"message": "429 in message", "code": "1308"}})
                   for code in (400, 401, 403, 404, 422)]
        actions.extend([(200, "not-json 429"), (200, {"error": {"code": "999", "message": "429"}})])
        for action in actions:
            with self.subTest(action=action):
                self.primary.actions.append(action)
                client = self.client()
                self.assertIsNone(client.send_message("hello"))
                self.assertTrue(client.last_error)
                self.assertEqual(client.last_primary_error, "")
                self.assertFalse(self.backup.requests)

    def test_empty_primary_does_not_switch_and_success_clears_error(self):
        self.primary.actions.extend([(200, answer("")), (200, answer("recovered"))])
        client = self.client()
        self.assertEqual(client.send_message("hello"), "recovered")
        self.assertEqual(client.last_error, "")
        self.assertFalse(self.backup.requests)

    def test_empty_backup_retry_stays_on_backup(self):
        self.primary.actions.append((429, {"error": {"code": "1302"}}))
        self.backup.actions.extend([(200, answer("")), (200, answer("backup"))])
        client = self.client()
        self.assertEqual(client.send_message("hello"), "backup")
        self.assertEqual((len(self.primary.requests), len(self.backup.requests)), (1, 2))
        self.assertEqual(client.last_error, "")

    def test_backup_failure_is_terminal(self):
        self.primary.actions.append((429, {"error": {"code": "1302"}}))
        self.backup.actions.append((403, {"error": {"message": "plan rejected"}}))
        client = self.client()
        self.assertIsNone(client.send_message("hello"))
        self.assertEqual((len(self.primary.requests), len(self.backup.requests)), (1, 1))
        self.assertEqual(client.last_provider, "mimo")
        self.assertIn("403", client.last_error)
        self.assertIn("429", client.last_primary_error)
        self.assertNotIn("fixture-", client.last_error)

    def test_enabled_requires_literal_bool_and_complete_config(self):
        for enabled in (False, "true", "false", 1, None):
            with self.subTest(enabled=enabled), override_settings(MIMO_API_CONFIG={**self.mimo, "enabled": enabled}):
                client = self.client()
                self.assertFalse(client.mimo.available)
                self.primary.actions.append((429, {"error": {"code": "1302"}}))
                self.assertIsNone(client.send_message("hello"))
                self.assertFalse(self.backup.requests)
        with override_settings(MIMO_API_CONFIG={**self.mimo, "api_key": ""}):
            self.assertFalse(self.client().mimo.available)

    def test_tool_history_and_reasoning_survive_mid_chain_switch(self):
        self.primary.actions.extend([(200, tool_answer()), (429, {"error": {"code": "1302"}})])
        self.backup.actions.extend([(200, tool_answer()), (200, tool_answer("lookup-2", "more reasoning")),
                                    (200, answer("done", reasoning_content="final reasoning"))])
        executed = []

        def handler(name, args):
            executed.append((name, args))
            return {"value": 99}

        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        client = self.client()
        self.assertEqual(client.send_message_with_tools("hello", tools, handler), "done")
        self.assertEqual(executed, [("lookup", {"id": 42}), ("lookup", {"id": 42})])
        first = self.backup.requests[0]["payload"]
        self.assertEqual([m["role"] for m in first["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(first["messages"][2]["reasoning_content"], "analysis")
        self.assertEqual(first["messages"][3]["tool_call_id"], "lookup-1")
        self.assertEqual(json.loads(first["messages"][3]["content"]), {"value": 99})
        self.assertEqual(first["tools"], tools)
        self.assertEqual(first["thinking"], {"type": "disabled"})
        self.assertEqual(first["max_completion_tokens"], client.max_tokens_tools)
        self.assertEqual(client.last_reasoning, "final reasoning")
        self.assertEqual((len(self.primary.requests), len(self.backup.requests)), (2, 3))

    def test_tool_failure_stops_without_provider_switch(self):
        self.primary.actions.extend([(200, tool_answer()), (429, {"error": {"code": "1302"}})])

        def handler(name, args):
            raise ValueError("business failure")

        client = self.client()
        self.assertIsNone(client.send_message_with_tools("hello", [], handler))
        self.assertIn("business failure", client.last_error)
        self.assertEqual(len(self.primary.requests), 1)
        self.assertFalse(self.backup.requests)

    def test_malformed_tool_arguments_do_not_execute_or_switch(self):
        response = tool_answer()
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "not json"
        self.primary.actions.append((200, response))
        executed = []
        client = self.client()
        self.assertIsNone(client.send_message_with_tools("hello", [], lambda *args: executed.append(args)))
        self.assertTrue(client.last_error)
        self.assertFalse(executed)
        self.assertFalse(self.backup.requests)


    def test_settings_refresh_on_reused_client_and_pin_during_tool_chain(self):
        client = self.client()
        changed = {**self.mimo, "model": "mimo-dynamic-fixture", "api_key": "rotated-fixture-key",
                   "request_timeout_seconds": 0.2}
        self.primary.actions.append((429, {"error": {"code": "1302"}}))
        with override_settings(MIMO_API_CONFIG=changed):
            self.assertEqual(client.send_message("refresh"), "ok")
            self.assertEqual(client.last_model, changed["model"])
            self.assertEqual(client.mimo.request_timeout_seconds, 0.2)
            self.assertEqual(self.backup.requests[-1]["headers"]["Authorization"], "Bearer rotated-fixture-key")
        self.primary.actions.extend([(200, tool_answer()), (429, {"error": {"code": "1302"}})])
        self.backup.actions.append((200, answer("pinned")))

        def handler(name, args):
            settings.MIMO_API_CONFIG = changed
            settings.ZHIYU_API_CONFIG = {**self.config, "model": "glm-next-call"}
            return "result"

        self.assertEqual(client.send_message_with_tools("pin", [], handler), "pinned")
        self.assertEqual(client.last_model, self.mimo["model"])
        self.assertEqual(self.backup.requests[-1]["headers"]["Authorization"], "Bearer fixture-mimo-key")
        self.assertEqual(client.send_message("new call"), "ok")
        self.assertEqual(client.last_model, "glm-next-call")
        with override_settings(MIMO_API_CONFIG={**changed, "enabled": False}):
            self.primary.actions.append((429, {"error": {"code": "1302"}}))
            self.assertIsNone(client.send_message("disabled now"))
            self.assertFalse(client.mimo.available)

    def test_explicit_thinking_and_strict_token_budget(self):
        self.primary.actions.append((429, {"error": {"code": "1302"}}))
        client = self.client()
        self.assertEqual(client.send_message("think", max_tokens=17, thinking_type="enabled"), "ok")
        payload = self.backup.requests[-1]["payload"]
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["max_completion_tokens"], 17)
        self.assertNotIn("temperature", payload)
        before = (len(self.primary.requests), len(self.backup.requests))
        self.assertIsNone(client.send_message("zero budget", max_tokens=0))
        self.assertEqual(before, (len(self.primary.requests), len(self.backup.requests)))

    def test_configured_retry_thresholds_are_used(self):
        with override_settings(MIMO_API_CONFIG={**self.mimo, "primary_connection_attempts": 2,
                                              "primary_rate_limit_attempts": 2}):
            self.primary.actions.extend(["disconnect", "disconnect"])
            self.assertEqual(self.client().send_message("connect threshold"), "ok")
            self.assertEqual(len(self.primary.requests), 2)
            self.primary.actions.extend([(429, {"error": {"code": "1302"}}), (200, answer("recovered"))])
            self.assertEqual(self.client().send_message("rate threshold"), "recovered")
            self.assertEqual(len(self.primary.requests), 4)
            self.assertEqual(len(self.backup.requests), 1)

    def test_tls_certificate_failure_never_switches(self):
        tls_primary = HTTPFixture(tls=True)
        self.addCleanup(tls_primary.close)
        with override_settings(ZHIYU_API_CONFIG={**self.config, "base_url": tls_primary.url}):
            client = self.client()
            self.assertIsNone(client.send_message("untrusted certificate"))
            self.assertIn("certificate verify failed", client.last_error.lower())
            self.assertEqual(client.last_provider, "glm")
            self.assertEqual(client.last_primary_error, "")
            self.assertFalse(self.backup.requests)

    def test_enabling_backup_preserves_glm_coding_endpoint(self):
        coding = HTTPFixture()
        self.addCleanup(coding.close)
        primary = {**self.config, "coding_base_url": coding.url, "model": "glm-4.7"}
        coding.actions.extend([(200, answer("coding")), (200, answer("coding"))])
        for enabled in (True, False):
            with self.subTest(enabled=enabled), override_settings(
                ZHIYU_API_CONFIG=primary, MIMO_API_CONFIG={**self.mimo, "enabled": enabled}
            ):
                self.assertEqual(self.client().send_message("normal request"), "coding")
        self.assertEqual(len(coding.requests), 2)
        self.assertFalse(self.primary.requests)
        self.assertFalse(self.backup.requests)

    def test_three_empty_primary_responses_do_not_switch(self):
        self.primary.actions.extend([(200, answer(""))] * 3)
        client = self.client()
        self.assertIsNone(client.send_message("empty"))
        self.assertEqual(len(self.primary.requests), 3)
        self.assertIn("empty content", client.last_error)
        self.assertFalse(self.backup.requests)


if __name__ == "__main__":
    unittest.main()
