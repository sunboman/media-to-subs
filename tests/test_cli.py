"""Command-line precedence, safe errors and invocation-scoped cancellation."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from media_to_subs import cli
from media_to_subs.errors import CancelledError, ConfigurationError, ProcessingError
from media_to_subs.pipeline import RunResult


class CLITests(unittest.TestCase):
    def invoke(self, args, *, environment=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,
                {"HOME": str(Path(temp).resolve()), **(environment or {})}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            # Legacy explicit-configuration tests bypass intentional saved state.
            code = cli.main([*args, "--no-profile"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_help_never_loads_config_or_requires_engines(self):
        with patch.object(cli, "load_config", side_effect=AssertionError("unexpected config load")):
            code, stdout, stderr = self.invoke(["--help"], environment={"MEDIA_TO_SUBS_STT_CPU_THREADS": "invalid"})
        self.assertEqual(code, 0)
        for flag in ("--stt", "--llm", "--whisper-model", "--llm-model", "--managed-llm",
                     "--save-source", "--include-media-context"):
            self.assertIn(flag, stdout)
        self.assertEqual(stderr, "")

    def test_version_requires_no_input_or_config(self):
        code, stdout, stderr = self.invoke(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("media-to-subs 0.1.0", stdout)
        self.assertEqual(stderr, "")

    def test_flags_override_toml_then_env_and_model_flags_are_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "providers.toml"
            config.write_text("[stt]\nmode='api'\ncpu_threads=3\n[llm]\nmode='api'\nmodel='served-name'\n"
                              "[run]\noverwrite=true\ninclude_media_context=true\n")
            with patch.object(cli, "run", return_value=RunResult(Path(temp) / "out.srt")) as run:
                code, stdout, stderr = self.invoke(["input.wav", "--config", str(config), "--stt", "local",
                    "--whisper-model", "whisper.bin", "--llm-model", "translate.gguf", "--no-overwrite",
                    "--no-include-media-context"], environment={"MEDIA_TO_SUBS_STT_CPU_THREADS": "8"})
            self.assertEqual(code, 0, stderr)
            _, settings, _, _ = run.call_args.args
            self.assertEqual(settings.stt.mode, "local")
            self.assertEqual(settings.stt.cpu_threads, 3)
            self.assertEqual(settings.llm.mode, "api")
            self.assertEqual(settings.llm.model, "served-name")
            self.assertEqual(settings.stt.model_path, Path("whisper.bin"))
            self.assertEqual(settings.llm.model_path, Path("translate.gguf"))
            self.assertFalse(settings.overwrite)
            self.assertFalse(settings.include_media_context)

    def test_stable_exit_codes_without_expected_tracebacks(self):
        for error, expected in ((ConfigurationError("Bad configuration"), 2),
                                (ProcessingError("Processing failed"), 1),
                                (CancelledError("Cancelled"), 130), (KeyboardInterrupt(), 130),
                                (PermissionError("private path"), 1)):
            with self.subTest(error=type(error).__name__), patch.object(cli, "run", side_effect=error):
                code, stdout, stderr = self.invoke(["input.wav"])
                self.assertEqual(code, expected, stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertNotIn("private path", stderr)

    def test_usage_and_unsafe_url_errors_do_not_echo_raw_values(self):
        for args in ([], ["input.wav", "--unknown-secret", "sensitive-value"],
                     ["input.wav", "--stt-url", "https://user:sensitive-value@example.invalid/path?q=secret"],
                     ["input.wav", "--stt", "sensitive-value"]):
            with self.subTest(args=args):
                code, stdout, stderr = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertNotIn("sensitive-value", stdout + stderr)
                self.assertNotIn("Traceback", stderr)

    def test_signal_handlers_cancel_and_restore_after_invocation(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in previous:
            def interrupt(media, settings, cancellation, progress):
                progress("Transcribing audio")
                signal.raise_signal(sig)
                cancellation.raise_if_cancelled()
                self.fail("Signal did not cancel the invocation")
            with self.subTest(sig=sig), patch.object(cli, "run", side_effect=interrupt):
                code, stdout, stderr = self.invoke(["input.wav"])
                self.assertEqual(code, 130)
                self.assertEqual(stdout, "")
                self.assertIn("Transcribing audio", stderr)
            self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)

    def test_sigterm_during_config_loading_is_also_an_invocation_cancellation(self):
        previous = signal.getsignal(signal.SIGTERM)
        escaped = []
        signal.signal(signal.SIGTERM, lambda *_: escaped.append(True))
        settings = cli.load_config(environ={})
        def load(*args, **kwargs):
            signal.raise_signal(signal.SIGTERM)
            return settings
        try:
            with patch.object(cli, "load_config", side_effect=load), patch.object(cli, "run") as run:
                code, _, stderr = self.invoke(["input.wav"])
                self.assertEqual(code, 130, stderr)
                self.assertEqual(escaped, [])
                run.assert_not_called()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_config_and_hardlink_alias_are_protected_before_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "providers.toml"
            config.write_text("[run]\noverwrite=true\n")
            alias = Path(temp) / "alias.srt"
            alias.hardlink_to(config)
            for flag, output in (("--output", config), ("--save-source", alias)):
                with self.subTest(flag=flag), patch.object(cli, "run") as run:
                    code, stdout, stderr = self.invoke(["input.wav", "--config", str(config), flag, str(output)])
                    self.assertEqual(code, 1, stderr)
                    run.assert_not_called()
            self.assertEqual(config.read_text(), "[run]\noverwrite=true\n")


if __name__ == "__main__":
    unittest.main()

class SavedCLITests(unittest.TestCase):
    def setUp(self):
        from test_profile import api_profile
        from media_to_subs.profile import profile_path, save_profile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config")}
        self.path = profile_path(self.env)
        save_profile(api_profile(), self.path)

    def invoke(self, args, environment=None):
        output = io.StringIO()
        with patch.dict(os.environ, {**self.env, **(environment or {})}, clear=True), redirect_stderr(output), redirect_stdout(output):
            code = cli.main(args)
        return code, output.getvalue()

    def test_bare_command_loads_saved_keys_without_mutating_environment(self):
        def run(media, config, cancellation, progress):
            self.assertEqual((config.stt.api_key, config.llm.api_key), ("fake-stt", "fake-llm"))
            self.assertNotIn("MEDIA_TO_SUBS_STT_API_KEY", os.environ)
            progress("selected fake-stt fake-llm")
        with patch.object(cli, "run", side_effect=run):
            code, message = self.invoke(["input.wav"])
        self.assertEqual(code, 0, message)
        self.assertNotIn("fake-", message)

    def test_endpoint_override_fails_before_pipeline_unless_explicit_key(self):
        for extra, env in ((["--stt-url", "https://different.invalid/v1"], {}),
                           ([], {"MEDIA_TO_SUBS_LLM_URL": "https://llm.invalid/other"})):
            with patch.object(cli, "run") as run:
                code, message = self.invoke(["input.wav", *extra], env)
            self.assertEqual(code, 2, message)
            run.assert_not_called()
        with patch.object(cli, "run") as run:
            code, message = self.invoke(["input.wav", "--stt-url", "https://different.invalid/v1"],
                                        {"MEDIA_TO_SUBS_STT_API_KEY": "explicit-key"})
        self.assertEqual(code, 0, message)
        self.assertEqual(run.call_args.args[1].stt.api_key, "explicit-key")

    def test_help_version_config_and_no_profile_never_read_saved_state(self):
        config_file = self.root / "config.toml"
        config_file.write_text("[stt]\nmode='api'\n")
        with patch.object(cli, "load_profile", side_effect=AssertionError("Profile was read")):
            for args in (["--help"], ["--version"], ["setup", "--help"], ["doctor", "--help"],
                         ["input.wav", "--no-profile"], ["input.wav", "--config", str(config_file)]):
                with self.subTest(args=args), patch.object(cli, "run") as run:
                    code, message = self.invoke(args)
                    self.assertEqual(code, 0, message)
                    if run.called:
                        self.assertIsNone(run.call_args.args[1].stt.api_key)
                        self.assertIsNone(run.call_args.args[1].llm.api_key)

    def test_profile_aliases_are_protected_even_when_loading_is_bypassed(self):
        alias = self.root / "alias.srt"
        alias.hardlink_to(self.path)
        for output in (self.path, alias):
            with patch.object(cli, "run") as run:
                code, message = self.invoke(["input.wav", "--no-profile", "--overwrite", "--output", str(output)])
            self.assertEqual(code, 1, message)
            run.assert_not_called()
        with patch.object(cli, "run") as run:
            code, message = self.invoke([str(self.path), "--no-profile"])
        self.assertEqual(code, 1, message)
        run.assert_not_called()

    def test_media_symlink_is_allowed_unless_it_aliases_profile(self):
        media = self.root / "input.wav"
        media.write_bytes(b"fixture")
        alias = self.root / "media-link.wav"
        alias.symlink_to(media)
        with patch.object(cli, "run") as run:
            code, message = self.invoke([str(alias)])
        self.assertEqual(code, 0, message)
        self.assertTrue(run.called)
        alias.unlink()
        alias.symlink_to(self.path)
        with patch.object(cli, "run") as run:
            code, message = self.invoke([str(alias)])
        self.assertEqual(code, 1, message)
        run.assert_not_called()

    def test_unconfigured_bare_command_guides_setup_and_explicit_flags_work(self):
        self.path.unlink()
        with patch.object(cli, "run") as run:
            code, message = self.invoke(["input.wav"])
            self.assertEqual(code, 2)
            self.assertIn("media-to-subs setup", message)
            run.assert_not_called()
            code, message = self.invoke(["input.wav", "--stt", "local"])
            self.assertEqual(code, 0, message)
            self.assertTrue(run.called)

    def test_doctor_redacts_secrets_duplicated_in_model_options(self):
        from media_to_subs.profile import load_profile, save_profile
        saved = load_profile(self.path)
        saved.settings["llm"]["model"] = "fake-stt-fake-llm"
        save_profile(saved, self.path)
        with patch("media_to_subs.doctor.shutil.which", return_value="/bin/true"), patch.object(cli, "run") as run:
            code, message = self.invoke(["doctor"])
        self.assertEqual(code, 0, message)
        self.assertNotIn("fake-", message)
        run.assert_not_called()
        with patch.object(cli, "run", side_effect=ProcessingError("model fake-stt-fake-llm failed")):
            code, message = self.invoke(["input.wav"])
        self.assertEqual(code, 1)
        self.assertNotIn("fake-", message)
