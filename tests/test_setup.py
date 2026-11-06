from contextlib import redirect_stderr, redirect_stdout
import getpass
import io
import os
from pathlib import Path
import tempfile
import signal
import unittest
from unittest.mock import patch
import warnings

from media_to_subs import cli
from media_to_subs.profile import load_profile, profile_path


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.model = self.root / "model.bin"
        self.model.write_bytes(b"model fixture")
        self.env = {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_DATA_HOME": str(self.root / "data")}
        self.output = io.StringIO()

    def invoke(self, args, *, answers=(), keys=(), environment=None, interactive=False):
        with patch.dict(os.environ, {**self.env, **(environment or {})}, clear=True), \
             redirect_stderr(self.output), redirect_stdout(self.output), \
             patch("sys.stdin.isatty", return_value=interactive), \
             patch("builtins.input", side_effect=answers), patch("getpass.getpass", side_effect=keys), \
             patch("media_to_subs.doctor.shutil.which", return_value="/bin/true"):
            return cli.main(["setup", *args])

    def local_args(self):
        return ["--stt", "local", "--llm", "local", "--whisper-model", str(self.model),
                "--vad-model", str(self.model), "--llm-model", str(self.model), "--yes"]

    def test_noninteractive_local_setup_and_rerun_are_complete(self):
        self.assertEqual(self.invoke(self.local_args()), 0, self.output.getvalue())
        saved = load_profile(profile_path(self.env))
        self.assertEqual(saved.settings["stt"]["model_path"], str(self.model))
        self.assertTrue(saved.settings["llm"]["managed_server"])
        self.assertEqual(saved.settings["stt"]["source_language"], "auto")
        self.assertEqual(saved.credentials, {})
        before = profile_path(self.env).read_bytes()
        self.assertEqual(self.invoke(["--yes"]), 0, self.output.getvalue())
        self.assertEqual(profile_path(self.env).read_bytes(), before)

    def test_interactive_local_local_and_api_api(self):
        answers = ["local", str(self.model), str(self.model), "local", "managed", str(self.model), "auto", "yes"]
        self.assertEqual(self.invoke([], answers=answers, interactive=True), 0, self.output.getvalue())
        answers = ["api", "https://stt.invalid/v1", "speech", "api", "https://llm.invalid/v1", "translation", "auto", "yes"]
        self.assertEqual(self.invoke([], answers=answers, keys=["fake-stt", "fake-llm"], interactive=True), 0, self.output.getvalue())
        saved = load_profile(profile_path(self.env))
        self.assertEqual(saved.credentials["stt"]["key"], "fake-stt")
        self.assertEqual(saved.credentials["llm"]["key"], "fake-llm")
        self.assertNotIn("fake-", self.output.getvalue())

    def test_cancel_does_not_change_existing_state(self):
        self.assertEqual(self.invoke(self.local_args()), 0)
        before = profile_path(self.env).read_bytes()
        answers = ["local", "", "", "local", "managed", "", "auto", "no"]
        self.assertEqual(self.invoke([], answers=answers, interactive=True), 130, self.output.getvalue())
        self.assertEqual(profile_path(self.env).read_bytes(), before)
        self.assertEqual(self.invoke([], answers=[EOFError()], interactive=True), 130)
        self.assertEqual(profile_path(self.env).read_bytes(), before)

    def test_api_automation_uses_env_keys_and_endpoint_changes_need_fresh_key(self):
        args = ["--stt", "api", "--llm", "api", "--stt-url", "https://stt.invalid/v1", "--stt-model-name", "speech",
                "--llm-url", "https://llm.invalid/v1", "--llm-model-name", "translate", "--yes"]
        env = {"MEDIA_TO_SUBS_STT_API_KEY": "fake-stt", "MEDIA_TO_SUBS_LLM_API_KEY": "fake-llm"}
        self.assertEqual(self.invoke(args, environment=env), 0, self.output.getvalue())
        before = profile_path(self.env).read_bytes()
        self.assertEqual(self.invoke(["--yes", "--stt-url", "https://changed.invalid/v1"]), 2)
        self.assertEqual(profile_path(self.env).read_bytes(), before)
        self.assertEqual(self.invoke(["--yes"]), 0)
        self.assertEqual(profile_path(self.env).read_bytes(), before)
        self.assertEqual(self.invoke(self.local_args()), 0)
        self.assertEqual(load_profile(profile_path(self.env)).credentials, {})

    def test_mixed_stages_and_explicit_no_key_loopback_only(self):
        args = ["--stt", "api", "--stt-url", "http://127.0.0.1:9100/v1", "--stt-model-name", "speech",
                "--stt-no-key", "--llm", "local", "--llm-model", str(self.model), "--yes"]
        self.assertEqual(self.invoke(args), 0, self.output.getvalue())
        saved = load_profile(profile_path(self.env))
        self.assertEqual((saved.settings["stt"]["mode"], saved.settings["llm"]["mode"]), ("api", "local"))
        self.assertEqual(self.invoke([*args, "--stt-url", "https://remote.invalid"]), 2)
        args = ["--stt", "local", "--whisper-model", str(self.model), "--vad-model", str(self.model),
                "--llm", "api", "--llm-url", "https://llm.invalid", "--llm-model-name", "translate", "--yes"]
        self.assertEqual(self.invoke(args, environment={"MEDIA_TO_SUBS_LLM_API_KEY": "fake-llm"}), 0)

    def test_getpass_echo_fallback_aborts_before_secret_entry(self):
        def unsafe_prompt(*args, **kwargs):
            warnings.warn("Password input may be echoed", getpass.GetPassWarning)
            self.fail("Must abort before echoed input")
        with patch.dict(os.environ, self.env, clear=True), patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["api", "https://stt.invalid", "speech"]), \
             patch("getpass.getpass", side_effect=unsafe_prompt), redirect_stderr(self.output):
            self.assertEqual(cli.main(["setup"]), 2)
        self.assertFalse(profile_path(self.env).exists())

    def test_missing_assets_and_noninteractive_keys_leave_no_profile(self):
        self.model.unlink()
        self.assertEqual(self.invoke(self.local_args()), 2, self.output.getvalue())
        self.assertFalse(profile_path(self.env).exists())
        args = ["--stt", "api", "--stt-url", "https://stt.invalid", "--stt-model-name", "speech", "--yes"]
        self.assertEqual(self.invoke(args), 2)
        self.assertFalse(profile_path(self.env).exists())

    def test_explicit_download_uses_packaged_manifest_and_data_root(self):
        from media_to_subs import model_setup
        downloaded = []
        def fetch(entry, root):
            downloaded.append((entry, root))
            root.mkdir(parents=True, exist_ok=True)
            destination = root / entry["filename"]
            destination.write_bytes(b"fixture")
            return destination
        args = ["--stt", "local", "--llm", "local", "--download-whisper", "--vad-model", str(self.model),
                "--llm-model", str(self.model), "--yes"]
        with patch.object(model_setup, "fetch_model", side_effect=fetch):
            self.assertEqual(self.invoke(args), 0, self.output.getvalue())
        self.assertEqual(len(downloaded), 1)
        entry, root = downloaded[0]
        self.assertEqual(root, self.root / "data/media-to-subs/models")
        self.assertIn(str(entry["bytes"]), self.output.getvalue())
        self.assertIn(entry["license"], self.output.getvalue())
        self.assertEqual(load_profile(profile_path(self.env)).settings["stt"]["model_path"], str(root / entry["filename"]))

    def test_signal_during_download_cancels_and_restores_handlers_without_save(self):
        from media_to_subs import model_setup
        self.assertEqual(self.invoke(self.local_args()), 0)
        previous = profile_path(self.env).read_bytes()
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in handlers:
            def interrupt(*args):
                signal.raise_signal(sig)
                self.fail("Download did not stop immediately")
            with patch.object(model_setup, "fetch_model", side_effect=interrupt):
                self.assertEqual(self.invoke(["--download-whisper", "--yes"]), 130, self.output.getvalue())
            self.assertEqual(profile_path(self.env).read_bytes(), previous)
            self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)


if __name__ == "__main__":
    unittest.main()
