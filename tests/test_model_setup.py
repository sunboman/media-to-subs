"""Offline protocol and file-safety checks for the explicit model downloader."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from media_to_subs import model_setup as setup

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_models.py"
DATA = b"reference model bytes\n" * 100
REVISION = "a" * 40


class ModelSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = []
        self.behavior = "normal"
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requested = self.headers.get("Range")
                test.requests.append(requested)
                if test.behavior == "error":
                    self.send_error(503)
                    return
                if test.behavior == "timeout":
                    time.sleep(0.15)
                body = DATA
                if test.behavior == "corrupt":
                    body = b"x" * len(DATA)
                start = int(requested[6:-1]) if requested else 0
                ranged = requested and test.behavior != "ignore_range"
                self.send_response(206 if ranged else 200)
                if ranged:
                    actual_start = start + 1 if test.behavior == "bad_range" else start
                    if test.behavior != "missing_range":
                        self.send_header("Content-Range", f"bytes {actual_start}-{len(DATA)-1}/{len(DATA)}")
                    body = body[start:]
                self.send_header("Content-Length", str(len(body) + (1 if test.behavior == "bad_length" else 0)))
                if test.behavior == "encoding":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if test.behavior == "short":
                    body = body[:100]
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.entry = {
            "filename": "model.bin", "revision": REVISION,
            "url": f"http://127.0.0.1:{self.server.server_port}/{REVISION}/model.bin",
            "sha256": hashlib.sha256(DATA).hexdigest(), "bytes": len(DATA),
            "license": "MIT", "license_url": "https://example.org/LICENSE",
            "source_url": "https://example.org/model",
        }
        self.dest = self.root / "model.bin"
        self.part = self.root / "model.bin.part"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def fetch(self, **kwargs):
        return setup.fetch_model(self.entry, self.root, **kwargs)

    def test_download_then_cache_validates_without_network(self):
        self.assertEqual(self.fetch(), self.dest)
        self.assertEqual(self.dest.read_bytes(), DATA)
        self.assertFalse(self.part.exists())
        self.behavior = "error"
        self.assertEqual(self.fetch(), self.dest)
        self.assertEqual(len(self.requests), 1)
        self.dest.write_bytes(b"x" * len(DATA))
        with self.assertRaises(setup.SetupError):
            self.fetch()
        self.assertEqual(self.dest.read_bytes(), b"x" * len(DATA))
        self.assertEqual(len(self.requests), 1)

    def test_corrupt_download_is_never_published(self):
        self.behavior = "corrupt"
        with self.assertRaises(setup.SetupError):
            self.fetch()
        self.assertFalse(self.dest.exists())

    def test_resume_download_sends_correct_offset(self):
        self.part.write_bytes(DATA[:400])
        self.fetch()
        self.assertEqual(self.requests, ["bytes=400-"])
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        self.part.write_bytes(DATA[:400])
        self.behavior = "ignore_range"
        self.fetch()
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_bad_or_missing_content_range_preserves_partial(self):
        for behavior in ("bad_range", "missing_range"):
            with self.subTest(behavior=behavior):
                self.part.write_bytes(DATA[:400])
                self.behavior = behavior
                with self.assertRaises(setup.SetupError):
                    self.fetch()
                self.assertEqual(self.part.read_bytes(), DATA[:400])
                self.assertFalse(self.dest.exists())

    def test_short_transfer_can_resume_on_next_invocation(self):
        self.behavior = "short"
        with self.assertRaises(setup.SetupError):
            self.fetch()
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.part.read_bytes(), DATA[:100])
        self.behavior = "normal"
        self.fetch()
        self.assertEqual(self.requests, [None, "bytes=100-"])
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_complete_partial_is_verified_without_network(self):
        self.part.write_bytes(DATA)
        self.fetch()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_symlink_and_hardlinked_partial_are_untouched(self):
        target = self.root / "user.bin"
        target.write_bytes(b"user file")
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.part.symlink_to(target)
                else:
                    self.part.hardlink_to(target)
                with self.assertRaises(setup.SetupError):
                    self.fetch()
                self.assertEqual(target.read_bytes(), b"user file")
                self.part.unlink()
        self.dest.symlink_to(target)
        with self.assertRaises(setup.SetupError):
            self.fetch()
        self.assertEqual(target.read_bytes(), b"user file")
        self.assertEqual(self.requests, [])

    def test_publication_race_does_not_clobber_existing_file(self):
        original_link = setup.os.link
        def competing_writer(source, destination, **kwargs):
            Path(destination).write_bytes(b"another process")
            return original_link(source, destination, **kwargs)
        with patch.object(setup.os, "link", side_effect=competing_writer):
            with self.assertRaises(setup.SetupError):
                self.fetch()
        self.assertEqual(self.dest.read_bytes(), b"another process")
        self.assertEqual(self.part.read_bytes(), DATA)

    def test_http_error_and_encoding_preserve_partial(self):
        for behavior in ("error", "encoding", "bad_length"):
            with self.subTest(behavior=behavior):
                self.part.write_bytes(DATA[:400])
                self.behavior = behavior
                with self.assertRaises(setup.SetupError):
                    self.fetch()
                self.assertEqual(self.part.read_bytes(), DATA[:400])
                self.assertFalse(self.dest.exists())

    def test_network_timeout_is_bounded(self):
        self.behavior = "timeout"
        with self.assertRaises(setup.SetupError):
            self.fetch(timeout=0.03)
        self.assertFalse(self.dest.exists())

    def test_corrupt_prefix_and_oversized_partial_are_never_published(self):
        for content in (b"x" * 400, DATA + b"extra"):
            with self.subTest(size=len(content)):
                self.part.write_bytes(content)
                with self.assertRaises(setup.SetupError):
                    self.fetch()
                self.assertFalse(self.dest.exists())
                self.assertTrue(self.part.read_bytes().startswith(content))

    def test_active_model_lock_fails_without_touching_partial(self):
        self.part.write_bytes(DATA[:400])
        with (self.root / "model.bin.lock").open("wb") as lock:
            setup.fcntl.flock(lock, setup.fcntl.LOCK_EX | setup.fcntl.LOCK_NB)
            with self.assertRaises(setup.SetupError):
                self.fetch()
        self.assertEqual(self.part.read_bytes(), DATA[:400])
        self.assertEqual(self.requests, [])
        self.fetch()  # Lock is released even though its inode persists.
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_explicit_selection_and_manifest_listing_do_not_download(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"schema_version": 1, "models": {"test": self.entry}}))
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(setup.main(["--manifest", str(path), "--list"]), 0)
            self.assertIn(REVISION, out.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(setup.main(["--manifest", str(path)]), 1)
            self.assertEqual(setup.main(["--manifest", str(path), "test", "unknown"]), 1)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.dest.exists())

    def test_checked_in_manifest_has_valid_pins(self):
        entries = setup.load_manifest(SCRIPT.parent.parent / "models-manifest.json")
        self.assertEqual(set(entries), {"whisper-small", "silero-v6.2.0", "qwen2.5-1.5b-instruct-q4-k-m"})

    def test_manifest_rejects_unpinned_or_unsafe_entries(self):
        path = self.root / "manifest.json"
        for changes in ({"filename": "../escape.bin"}, {"revision": "main"},
                        {"sha256": "bad"}, {"bytes": -1},
                        {"url": "https://name:secret@example.org/file"}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({"schema_version": 1, "models": {"test": self.entry | changes}}))
                with self.assertRaises(setup.SetupError):
                    setup.load_manifest(path)
        path.write_text(json.dumps({"schema_version": 1, "models": {"test": self.entry}}))
        self.assertEqual(setup.load_manifest(path)["test"], self.entry)


if __name__ == "__main__":
    unittest.main()
