"""Local sockets exercise requests, subprocess cancellation and transport policy."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import traceback
import unittest
from unittest.mock import patch

from media_to_subs.config import LLMConfig
from media_to_subs.errors import CancelledError, ConfigurationError, ProviderError
from media_to_subs.llm import ChatClient, TranslationValidationError
from media_to_subs.runtime import Cancellation


class HTTPFixture:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.started = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size)
                fixture.requests.append((self.path, dict(self.headers), json.loads(body)))
                fixture.started.set()
                response = fixture.responses.pop(0) if fixture.responses else (500, b"unexpected call", {})
                if callable(response):
                    response = response(self)
                    if response is None:
                        return
                status, raw, headers = response
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def chat_response(content='{"translations":[{"id":"a","text":"你好"}]}', **extra):
    choice = {"message": {"content": content}, "finish_reason": "stop"}
    choice.update(extra)
    return (200, json.dumps({"choices": [choice]}).encode(), {"Content-Type": "application/json"})


class HTTPTests(unittest.TestCase):
    def config(self, server, **options):
        return LLMConfig(url=server.url, model="synthetic", connect_timeout=0.5,
                         read_timeout=2, **options)

    def chat(self, config, cancellation=None, progress=None):
        return ChatClient(config, cancellation or Cancellation(), progress or (lambda _: None)).chat(
            [{"role": "user", "content": "synthetic"}])

    def test_exact_endpoint_explicit_key_no_proxy_or_netrc(self):
        with HTTPFixture([chat_response(), chat_response()]) as server:
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "", "OPENAI_API_KEY": "wrong-key"}):
                self.chat(self.config(server, api_key="selected-key"))
                self.chat(LLMConfig(url=server.url + "/chat/completions", model="other", read_timeout=2))
            self.assertEqual([r[0] for r in server.requests], ["/v1/chat/completions"] * 2)
            self.assertEqual(server.requests[0][1]["Authorization"], "Bearer selected-key")
            self.assertNotIn("Authorization", server.requests[1][1])
            self.assertEqual(server.requests[0][2]["model"], "synthetic")
            self.assertFalse(server.requests[0][2]["stream"])

    def test_transient_status_retry_budget_and_auth_no_retry(self):
        with HTTPFixture([(503, b"private-body", {}), chat_response()]) as server:
            events = []
            self.chat(self.config(server, mode="api", max_retries=1), progress=events.append)
            self.assertEqual(len(server.requests), 2)
            self.assertTrue(events)
        for status in (401, 403, 400, 404, 422, 501):
            with self.subTest(status=status), HTTPFixture([(status, b"private-body", {})]) as server:
                with self.assertRaises(ProviderError) as raised:
                    self.chat(self.config(server, mode="api", max_retries=3))
                self.assertEqual(len(server.requests), 1)
                self.assertNotIn("private-body", str(raised.exception))
        with HTTPFixture([(429, b"", {})] * 3) as server:
            with self.assertRaises(ProviderError):
                self.chat(self.config(server, max_retries=1))
            self.assertEqual(len(server.requests), 2)

    def test_redirect_not_followed_and_no_secrets_in_traceback(self):
        with HTTPFixture([chat_response()]) as destination:
            with HTTPFixture([(307, b"private-body", {"Location": destination.url + "/secret-token"})]) as server:
                with self.assertRaises(ProviderError) as raised:
                    self.chat(self.config(server, api_key="secret-token", max_retries=3))
                rendered = ''.join(traceback.format_exception(raised.exception))
                for private in ("secret-token", "private-body", server.url, destination.url):
                    self.assertNotIn(private, rendered)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(destination.requests, [])

    def test_bad_envelope_and_oversized_response(self):
        for response in [(200, b"private-body invalid JSON", {}),
                         (200, b'{"choices":[]}', {}),
                         chat_response("partial", finish_reason="length")]:
            with self.subTest(response=response), HTTPFixture([response]) as server:
                with self.assertRaises(TranslationValidationError):
                    self.chat(self.config(server))
        with HTTPFixture([(200, b'x' * (2 * 1024 * 1024 + 1), {})]) as server:
            with self.assertRaises(ProviderError):
                self.chat(self.config(server))
            self.assertEqual(len(server.requests), 1)

    def test_stalled_headers_cancel_promptly_and_no_retry(self):
        release = threading.Event()
        def stall(handler):
            release.wait(5)
            return None
        try:
            with HTTPFixture([stall]) as server:
                cancellation = Cancellation()
                def cancel_started():
                    if server.started.wait(3):
                        cancellation.cancel()
                watcher = threading.Thread(target=cancel_started)
                watcher.start()
                start = time.monotonic()
                with self.assertRaises(CancelledError):
                    self.chat(self.config(server, max_retries=3), cancellation)
                watcher.join(3)
                self.assertLess(time.monotonic() - start, 2)
                self.assertEqual(len(server.requests), 1)
        finally:
            release.set()

    def test_slow_trickle_has_wall_clock_deadline_and_no_retry(self):
        def trickle(handler):
            handler.send_response(200)
            handler.end_headers()
            try:
                for _ in range(100):
                    handler.wfile.write(b' ')
                    handler.wfile.flush()
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return None
        with HTTPFixture([trickle]) as server:
            config = LLMConfig(url=server.url, model="synthetic", connect_timeout=0.3,
                               read_timeout=0.4, max_retries=3)
            start = time.monotonic()
            with self.assertRaises(ProviderError):
                self.chat(config)
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(len(server.requests), 1)

    def test_cancel_during_retry_delay_sends_no_second_request(self):
        token = Cancellation()
        with HTTPFixture([(503, b"", {}), chat_response()]) as server:
            with self.assertRaises(CancelledError):
                self.chat(self.config(server, max_retries=3), token, lambda _: token.cancel())
            self.assertEqual(len(server.requests), 1)

    def test_read_timeout_without_response_never_retries(self):
        release = threading.Event()
        def stall(handler):
            release.wait(3)
            return None
        try:
            with HTTPFixture([stall]) as server:
                config = LLMConfig(url=server.url, model="synthetic", connect_timeout=0.5,
                                   read_timeout=0.2, max_retries=3)
                start = time.monotonic()
                with self.assertRaises(ProviderError):
                    self.chat(config)
                self.assertLess(time.monotonic() - start, 2)
                self.assertEqual(len(server.requests), 1)
        finally:
            release.set()

    def test_precancel_and_configuration_reject_without_call(self):
        cancellation = Cancellation()
        cancellation.cancel()
        with HTTPFixture([chat_response()]) as server:
            with self.assertRaises(CancelledError):
                self.chat(self.config(server), cancellation)
            for config in [LLMConfig(mode="api", model="synthetic"), LLMConfig(mode="api", url=server.url)]:
                with self.assertRaises(ConfigurationError):
                    self.chat(config)
            self.assertEqual(server.requests, [])
        with self.assertRaises(ConfigurationError):
            LLMConfig(url="http://example.com/v1")


if __name__ == "__main__":
    unittest.main()
