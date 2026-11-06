"""Real process/socket fixtures for managed ownership, readiness and failures."""

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

from media_to_subs.config import LLMConfig
from media_to_subs.errors import CancelledError, ConfigurationError, ProviderError
from media_to_subs.llm import ChatClient
from media_to_subs.runtime import Cancellation
from media_to_subs.translator import translate
from media_to_subs.types import Segment, TimedTranscript
from test_llm_http import HTTPFixture, chat_response


FAKE_SERVER = r'''
import json, os, pathlib, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
root = pathlib.Path(__file__).parent
mode = MODE
(root / "pid").write_text(str(os.getpid()))
(root / "args").write_text(json.dumps(sys.argv[1:]))
if mode == "exit":
    print("private-server-body", file=sys.stderr)
    sys.exit(7)
if mode == "stall":
    time.sleep(30)

def option(name):
    return sys.argv[sys.argv.index(name) + 1]
alias = option("--alias")
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if self.path == "/health":
            self.send(200, {"status": "ok"})
        elif self.path == "/v1/models":
            self.send(200, {"data": [{"id": "unowned" if mode == "wrong" else alias}]})
        else:
            self.send(404, {})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        (root / "post").write_text(json.dumps(body))
        if mode == "hangchat":
            time.sleep(30)
        if self.headers.get("Authorization") != "Bearer " + os.environ.get("LLAMA_API_KEY", ""):
            if os.environ.get("LLAMA_API_KEY"):
                self.send(401, {})
                return
        data = json.loads(body["messages"][1]["content"])
        content = json.dumps({"translations": [{"id": cue["id"], "text": "你好"} for cue in data["subtitles"]]})
        self.send(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
HTTPServer((option("--host"), int(option("--port"))), Handler).serve_forever()
'''


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = TimedTranscript("en", (Segment("a", 0, 1, "Hello"),), {})

    def config(self, mode="ready", **options):
        binary = self.root / "fake-llama"
        binary.write_text(f"#!{sys.executable}\n" + FAKE_SERVER.replace("mode = MODE", "mode = " + repr(mode)))
        binary.chmod(0o700)
        model = self.root / "synthetic.gguf"
        model.write_bytes(b"fixture")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        return LLMConfig(managed_server=True, server_binary=str(binary), model_path=model,
                         url=f"http://127.0.0.1:{port}/v1", startup_timeout=3,
                         connect_timeout=0.25, read_timeout=3, **options)

    def run_translation(self, config, token=None):
        return translate(self.source, config, None, token or Cancellation(), lambda _: None)

    def assert_child_gone(self):
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_owned_server_readiness_identity_flags_key_and_cleanup(self):
        config = self.config(api_key="synthetic-key")
        result = self.run_translation(config)
        self.assertEqual(result.segments[0].text, "你好")
        args = json.loads((self.root / "args").read_text())
        for flag, value in [("--ctx-size", "2048"), ("--threads", "4"), ("--gpu-layers", "0"), ("--host", "127.0.0.1")]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--offline", args)
        self.assertNotIn("synthetic-key", str(args))
        self.assertNotIn("synthetic-key", repr(result.provenance))
        self.assertEqual(json.loads((self.root / "post").read_text())["model"], args[args.index("--alias") + 1])
        self.assert_child_gone()

    def test_models_probe_transport_failure_retries_within_startup_deadline(self):
        from unittest.mock import patch
        from media_to_subs import _llm_server
        original = _llm_server.request_json
        failures = []
        def transient(url, **kwargs):
            if url.endswith('/v1/models') and not failures:
                failures.append(url)
                raise ProviderError('synthetic transient readiness timeout')
            return original(url, **kwargs)
        with patch.object(_llm_server, 'request_json', side_effect=transient):
            result = self.run_translation(self.config())
        self.assertEqual(len(failures), 1)
        self.assertEqual(result.segments[0].text, '你好')
        self.assert_child_gone()

    def test_delayed_helper_start_uses_remaining_startup_budget(self):
        from unittest.mock import patch
        from media_to_subs import _llm_transport
        original = _llm_transport.run_process
        probes = []
        def delayed_helper(args, **kwargs):
            request = json.loads(kwargs["input_text"])
            if request["method"] == "GET":
                probes.append(request)
                # Delay inside the actual owned subprocess, before importing the
                # real HTTP helper. This consumes the bridge wall-clock budget.
                args = [sys.executable, "-I", "-c",
                        "import time, runpy; time.sleep(1); "
                        "runpy.run_module('media_to_subs._llm_http', run_name='__main__')"]
            return original(args, **kwargs)
        config = replace(self.config(), startup_timeout=12)
        with patch.object(_llm_transport, "run_process", side_effect=delayed_helper):
            result = self.run_translation(config)
        self.assertEqual(result.segments[0].text, "你好")
        self.assertTrue(any(probe["url"].endswith("/v1/models") for probe in probes))
        self.assertTrue(all(probe["connect_timeout"] <= .25 and probe["read_timeout"] <= .5 for probe in probes))
        args = json.loads((self.root / "args").read_text())
        self.assertEqual(json.loads((self.root / "post").read_text())["model"], args[args.index("--alias") + 1])
        self.assert_child_gone()

    def test_existing_listener_survives_and_no_owned_process_started(self):
        config = self.config()
        with HTTPFixture([chat_response()]) as server:
            with self.assertRaises(ProviderError):
                self.run_translation(replace(config, url=server.url))
            self.assertFalse((self.root / "pid").exists())
            self.assertTrue(server.thread.is_alive())
            ChatClient(LLMConfig(url=server.url), Cancellation(), lambda _: None).chat([])
            self.assertEqual(len(server.requests), 1)

    def test_failure_and_wrong_identity_never_translate_or_leave_process(self):
        for mode in ("exit", "wrong"):
            with self.subTest(mode=mode):
                with self.assertRaises(ProviderError) as raised:
                    self.run_translation(self.config(mode))
                self.assertNotIn("private-server-body", str(raised.exception))
                self.assertFalse((self.root / "post").exists())
                self.assert_child_gone()

    def test_startup_deadline_and_cancellation_cleanup(self):
        config = replace(self.config("stall"), startup_timeout=0.5)
        start = time.monotonic()
        with self.assertRaises(ProviderError):
            self.run_translation(config)
        self.assertLess(time.monotonic() - start, 2)
        self.assert_child_gone()
        (self.root / "pid").unlink()
        token = Cancellation()
        stop = threading.Event()
        def cancel_started():
            while not stop.wait(0.01):
                if (self.root / "pid").exists():
                    token.cancel()
                    return
        watcher = threading.Thread(target=cancel_started)
        watcher.start()
        try:
            with self.assertRaises(CancelledError):
                self.run_translation(self.config("stall"), token)
            self.assert_child_gone()
        finally:
            stop.set()
            watcher.join(2)

    def test_cancellation_during_chat_closes_owned_server(self):
        config = self.config("hangchat")
        token = Cancellation()
        stop = threading.Event()
        def cancel_started():
            while not stop.wait(0.01):
                if (self.root / "post").exists():
                    token.cancel()
                    return
        watcher = threading.Thread(target=cancel_started)
        watcher.start()
        try:
            with self.assertRaises(CancelledError):
                self.run_translation(config, token)
            self.assert_child_gone()
        finally:
            stop.set()
            watcher.join(2)

    def test_requires_model_http_and_supported_path_only_when_needed(self):
        config = self.config()
        for invalid in [replace(config, model_path=None), replace(config, model_path=self.root / "missing"),
                        replace(config, url="https://127.0.0.1:12345/v1"),
                        replace(config, url="http://127.0.0.1:12345/wrong")]:
            with self.subTest(config=invalid), self.assertRaises(ConfigurationError):
                self.run_translation(invalid)
        self.assertFalse((self.root / "pid").exists())


if __name__ == "__main__":
    unittest.main()
