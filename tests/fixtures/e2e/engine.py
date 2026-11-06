"""Synthetic executable fixtures, never real model or speech quality evidence."""

import json
import os
from pathlib import Path
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # Synthetic readiness must not depend on external reverse DNS.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


SRT = "7\n00:00:00,125 --> 00:00:01,500\nこんにちは、これは合成テストです\n\n19\n00:00:02,000 --> 00:00:03,875\nありがとうございます、また会いましょう\n"


def event(kind):
    destination = os.environ.get("MTS_FIXTURE_EVENTS")
    if destination:
        with open(destination, "a") as stream:
            stream.write(json.dumps({"event": kind, "pid": os.getpid()}) + "\n")


def argument(*names, default=None):
    for name in names:
        if name in sys.argv:
            return sys.argv[sys.argv.index(name) + 1]
    return default


def whisper():
    event("whisper-start")
    if os.environ.get("MTS_FIXTURE_BLOCK") == "whisper":
        while True:
            time.sleep(.05)
    if "-dl" in sys.argv or "--detect-language" in sys.argv:
        print("auto-detected language: ja (p = 0.99)")
    else:
        output = argument("-of", "--output-file")
        if not output:
            raise SystemExit(3)
        Path(output + ".srt").write_text(SRT, encoding="utf-8")
    event("whisper-finished")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def respond(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond({"status": "ok", "data": [{"id": getattr(self.server, "alias", "fixture")}]})

    def do_POST(self):
        self.server.paths.append(self.path)
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.endswith("/audio/transcriptions"):
            self.respond({"language": "japanese", "segments": [
                {"start": .125, "end": 1.5, "text": "こんにちは、これは合成テストです"},
                {"start": 2, "end": 3.875, "text": "ありがとうございます、また会いましょう"},
            ]})
            return
        payload = json.loads(raw)
        content = json.loads(payload["messages"][-1]["content"])
        self.server.payloads.append(content)
        event("llm-request")
        if os.environ.get("MTS_FIXTURE_BLOCK") == "llm":
            while True:
                time.sleep(.05)
        translations = [{"id": cue["id"], "text": "你好，这是合成测试。"}
                        for cue in content["subtitles"]]
        if getattr(self.server, "malformed", False):
            translations = []
        self.respond({"choices": [{"message": {"content": json.dumps(
            {"translations": translations}, ensure_ascii=False)}}]})


def llama():
    event("llama-start")
    server = LoopbackServer((argument("--host", default="127.0.0.1"),
                                  int(argument("--port"))), Handler)
    server.daemon_threads = True
    server.payloads = []
    server.paths = []
    server.alias = argument("--alias", default="fixture")
    server.serve_forever(poll_interval=.05)


if __name__ == "__main__":
    if sys.argv[1] == "whisper":
        whisper()
    elif sys.argv[1] == "llama":
        llama()
    else:
        raise SystemExit(2)
