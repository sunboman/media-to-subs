"""Real CLI/FFmpeg/HTTP/process boundaries with synthetic inference fixtures."""

from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from media_to_subs.subtitles import parse_srt

FIXTURE = Path(__file__).parent / "fixtures/e2e/engine.py"
spec = importlib.util.spec_from_file_location("e2e_engine", FIXTURE)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("ffmpeg"), "E2E verification requires ffmpeg")
        self.assertIsNotNone(shutil.which("ffprobe"), "E2E verification requires ffprobe")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.media = self.root / "synthetic.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=16000:duration=4", "-y", str(self.media)],
                       check=True, capture_output=True, timeout=10)
        self.media_hash = self.digest(self.media)
        self.output = self.root / "translated.srt"
        self.events_path = self.root / "events.jsonl"
        self.model = self.root / "fixture-model.bin"
        self.model.write_bytes(b"synthetic model placeholder")
        self.engines = {}
        for kind in ("whisper", "llama"):
            binary = self.root / ("fake-" + kind)
            binary.write_text(f"#!{sys.executable}\nimport os, pathlib, runpy, sys\n"
                              f"os.environ['MTS_FIXTURE_EVENTS'] = {str(self.events_path)!r}\n"
                              f"block = pathlib.Path({str(self.root / 'block.txt')!r})\n"
                              "os.environ['MTS_FIXTURE_BLOCK'] = block.read_text() if block.exists() else ''\n"
                              f"sys.argv.insert(1, {kind!r})\n"
                              f"runpy.run_path({str(FIXTURE)!r}, run_name='__main__')\n")
            binary.chmod(0o700)
            self.engines[kind] = binary
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.Handler)
        self.server.daemon_threads = True
        self.server.payloads = []
        self.server.paths = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.stack.callback(self.close_server)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("MEDIA_TO_SUBS_") and key not in {"MTS_FIXTURE_BLOCK"}}
        self.env["MTS_FIXTURE_EVENTS"] = str(self.events_path)
        # No real credentials are used, even when the user has them configured.
        self.env["MEDIA_TO_SUBS_STT_API_KEY"] = "synthetic-stt-key"
        self.env["MEDIA_TO_SUBS_LLM_API_KEY"] = "synthetic-llm-key"
        self.scratch_parent = self.root / "scratch"
        self.scratch_parent.mkdir()
        self.env["TMPDIR"] = str(self.scratch_parent)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def events(self):
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def command(self, stt="api", llm="api", *, managed=False, extra=()):
        endpoint = self.endpoint
        if managed:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            endpoint = f"http://127.0.0.1:{port}/v1"
        config = self.root / "providers.toml"
        config.write_text(
            "[stt]\n" + f"url = {json.dumps(self.endpoint)}\n"
            f"whisper_binary = {json.dumps(str(self.engines['whisper']))}\n"
            "source_language = 'ja'\nmax_retries = 0\nchunk_duration = 30\n"
            "[llm]\nmodel = 'fixture'\nmax_retries = 0\nread_timeout = 5\nstartup_timeout = 5\n"
            + f"url = {json.dumps(endpoint)}\nserver_binary = {json.dumps(str(self.engines['llama']))}\n")
        command = [sys.executable, "-m", "media_to_subs.cli", str(self.media),
                   "--config", str(config), "--stt", stt, "--llm", llm,
                   "--output", str(self.output)]
        if stt == "local":
            command += ["--whisper-model", str(self.model)]
        if managed:
            command += ["--managed-llm", "--llm-model", str(self.model)]
        return command + list(extra)

    def invoke(self, command):
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=25)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("synthetic-stt-key", result.stdout + result.stderr)
        self.assertNotIn("synthetic-llm-key", result.stdout + result.stderr)
        self.assertEqual(self.digest(self.media), self.media_hash)
        self.assertEqual(self.model.read_bytes(), b"synthetic model placeholder")
        self.assertEqual(list(self.scratch_parent.iterdir()), [])
        return result

    def test_all_four_backend_combinations(self):
        for stt, llm in (("local", "local"), ("local", "api"), ("api", "local"), ("api", "api")):
            with self.subTest(stt=stt, llm=llm):
                self.events_path.unlink(missing_ok=True)
                self.output.unlink(missing_ok=True)
                export = self.root / "source.srt"
                export.unlink(missing_ok=True)
                result = self.invoke(self.command(stt, llm, managed=llm == "local",
                    extra=("--save-source", str(export))))
                self.assertEqual(result.returncode, 0, result.stderr)
                target = parse_srt(self.output.read_text(), language="zh")
                source = parse_srt(export.read_text(), language="ja")
                self.assertEqual([(c.id, c.start, c.end) for c in target.segments],
                                 [(c.id, c.start, c.end) for c in source.segments])
                self.assertEqual(len(target.segments), 2)
                self.assertTrue(all("你好" in cue.text for cue in target.segments))
                events = [event["event"] for event in self.events()]
                self.assertEqual("whisper-start" in events, stt == "local")
                self.assertEqual("llama-start" in events, llm == "local")
                if stt == llm == "local":
                    self.assertLess(events.index("whisper-finished"), events.index("llama-start"))
                    self.assertEqual(self.server.paths, [])
                for event in self.events():
                    if event["event"] == "llama-start":
                        with self.assertRaises(ProcessLookupError):
                            os.kill(event["pid"], 0)

    def test_actual_chinese_discovery_reuses_exact_authored_bytes_without_engines(self):
        sidecar = self.media.with_suffix(".zh.srt")
        text = ("7\n00:00:00,125 --> 00:00:01,500\n這是一段完整的合成中文測試\n\n"
                "19\n00:00:02,000 --> 00:00:03,875\n感謝觀看，這是第二句測試字幕\n")
        original = b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")
        sidecar.write_bytes(original)
        for binary in self.engines.values():
            binary.unlink()
        command = self.command("local", "local", managed=True, extra=("--output", str(sidecar),
            "--whisper-model", str(self.root / "missing-whisper"), "--llm-model", str(self.root / "missing-llm")))
        result = self.invoke(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.server.paths, [])

    def test_source_discovery_skips_whisper_and_force_uses_audio(self):
        sidecar = self.media.with_suffix(".ja.srt")
        sidecar.write_text(fixture.SRT)
        original = self.digest(sidecar)
        self.engines["whisper"].unlink()
        result = self.invoke(self.command("local", "api"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.paths, ["/v1/chat/completions"])
        self.assertNotIn("media_context", self.server.payloads[0])
        self.output.unlink()
        self.server.paths.clear()
        result = self.invoke(self.command("api", "api", extra=("--force-transcribe", "--include-media-context")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/v1/audio/transcriptions", self.server.paths)
        self.assertEqual(self.server.payloads[-1]["media_context"], f"{self.root.name} / synthetic")
        self.assertEqual(self.digest(sidecar), original)

    def test_invalid_and_partial_tracks_fall_back_and_remain_unchanged(self):
        invalid = self.media.with_suffix(".ja.srt")
        partial = self.media.with_suffix(".ja.signs.srt")
        invalid.write_bytes(b"not a subtitle file")
        partial.write_text(fixture.SRT)
        originals = {path: self.digest(path) for path in (invalid, partial)}
        result = self.invoke(self.command())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/v1/audio/transcriptions", self.server.paths)
        self.assertEqual({path: self.digest(path) for path in originals}, originals)

    def test_embedded_ass_paragraphs_and_numeric_dialogue_survive_source_export(self):
        authored = self.root / "authored.ass"
        authored.write_text(
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.10,0:00:01.50,Default,,0,0,0,,こんにちは、合成テストです\\N\\N123\\N次の段落も残します\n"
            "Dialogue: 0,0:00:02.00,0:00:03.80,Default,,0,0,0,,これは二番目の合成字幕です\n",
            encoding="utf-8")
        original = self.digest(authored)
        embedded = self.root / "synthetic.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.media), "-f", "ass", "-i", str(authored),
                        "-map", "0:a", "-map", "1:0", "-c:a", "pcm_s16le", "-c:s", "ass",
                        "-metadata:s:s:0", "language=jpn", "-y", str(embedded)],
                       check=True, capture_output=True, timeout=10)
        self.media = embedded
        self.media_hash = self.digest(embedded)
        self.engines["whisper"].unlink()
        export = self.root / "source.srt"
        result = self.invoke(self.command("local", "api", extra=("--save-source", str(export))))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Selected embedded subtitles", result.stderr)
        source = parse_srt(export.read_text(), language="ja", allow_overlaps=True)
        self.assertEqual(len(source.segments), 2)
        self.assertIn("\n\n123\n", source.segments[0].text)
        self.assertEqual([(cue.start, cue.end) for cue in source.segments], [(.1, 1.5), (2, 3.8)])
        self.assertEqual(self.server.paths, ["/v1/chat/completions"])
        self.assertEqual(self.digest(authored), original)
        self.assertEqual(self.events(), [])

    def test_real_digital_silence_publishes_nothing_and_uses_no_inference(self):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                        "-t", "1", "-y", str(self.media)], check=True, capture_output=True, timeout=10)
        self.media_hash = self.digest(self.media)
        export = self.root / "source.srt"
        for binary in self.engines.values():
            binary.unlink()
        result = self.invoke(self.command("local", "local", managed=True,
                            extra=("--save-source", str(export))))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No speech", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(export.exists())
        self.assertEqual(self.events(), [])
        self.assertEqual(self.server.paths, [])

    def test_existing_endpoint_survives_success_and_malformed_response(self):
        result = self.invoke(self.command("api", "local"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("llama-start", [e["event"] for e in self.events()])
        previous = self.output.read_bytes()
        self.server.malformed = True
        result = self.invoke(self.command("api", "local", extra=("--overwrite",)))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertTrue(self.thread.is_alive())

    def test_managed_translation_cancel_cleans_owned_process_and_preserves_inputs(self):
        sidecar = self.media.with_suffix(".ja.srt")
        sidecar.write_text(fixture.SRT)
        original = sidecar.read_bytes()
        self.output.write_bytes(b"previous complete output")
        (self.root / "block.txt").write_text("llm")
        process = subprocess.Popen(self.command("local", "local", managed=True, extra=("--overwrite",)),
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if any(e["event"] == "llm-request" for e in self.events()):
                    break
                if process.poll() is not None:
                    break
                time.sleep(.025)
            else:
                self.fail("Managed translation never reached its request")
            self.assertIsNone(process.poll(), process.communicate() if process.poll() is not None else None)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(self.output.read_bytes(), b"previous complete output")
            self.assertEqual(sidecar.read_bytes(), original)
            self.assertEqual(self.digest(self.media), self.media_hash)
            self.assertEqual(list(self.scratch_parent.iterdir()), [])
            self.assertTrue(self.thread.is_alive())
            for event in self.events():
                if event["event"] == "llama-start":
                    with self.assertRaises(ProcessLookupError):
                        os.kill(event["pid"], 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
