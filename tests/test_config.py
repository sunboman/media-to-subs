from dataclasses import FrozenInstanceError
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_to_subs.config import LLMConfig, RunConfig, STTConfig, load_config
from media_to_subs.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_saved_defaults_are_explicit_and_lower_than_environment(self):
        config = load_config(defaults={"stt": {"cpu_threads": 6}, "llm": {"batch_size": 7}},
                             cli={"stt": {"cpu_threads": 8}},
                             environ={"MEDIA_TO_SUBS_LLM_BATCH_SIZE": "4"})
        self.assertEqual((config.stt.cpu_threads, config.llm.batch_size), (8, 4))

    def test_repr_redacts_keys_repeated_in_other_fields(self):
        secret = "synthetic-credential"
        config = RunConfig(stt=STTConfig(mode="api", api_key=secret),
                           llm=LLMConfig(model=secret + "-model"))
        self.assertNotIn(secret, repr(config))
        self.assertNotIn(secret, repr(STTConfig(api_key=secret, model=secret)))

    def test_defaults_are_independent_immutable_and_do_not_require_models(self):
        config = load_config(environ={})
        self.assertEqual(config.stt.max_upload_bytes, 24_000_000)
        self.assertIsNone(config.stt.model_path)
        self.assertIsNone(config.llm.model_path)
        with self.assertRaises(FrozenInstanceError):
            config.stt.mode = "api"
        explicit = RunConfig(stt=STTConfig(mode="api"), llm=LLMConfig(mode="local"))
        self.assertEqual((explicit.stt.mode, explicit.llm.mode), ("api", "local"))

    def test_precedence_is_fieldwise_cli_file_env_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "providers.toml"
            filename.write_text('[stt]\nmode="api"\ncpu_threads=6\nmodel_path="models/test.bin"\n'
                                '[llm]\nbatch_size=3\n[run]\noverwrite=true\n', encoding="utf-8")
            config = load_config(filename, cli={"stt": {"cpu_threads": 8}, "run": {"overwrite": False}},
                                 environ={"MEDIA_TO_SUBS_STT_MODE": "local",
                                          "MEDIA_TO_SUBS_STT_CPU_THREADS": "2",
                                          "MEDIA_TO_SUBS_LLM_MODE": "api"})
            self.assertEqual((config.stt.mode, config.stt.cpu_threads), ("api", 8))
            self.assertEqual((config.llm.mode, config.llm.batch_size), ("api", 3))
            self.assertFalse(config.overwrite)
            self.assertEqual(config.stt.model_path, filename.parent / "models/test.bin")

    def test_only_surviving_environment_values_are_converted(self):
        invalid_env = {"MEDIA_TO_SUBS_LLM_BATCH_SIZE": "invalid",
                       "MEDIA_TO_SUBS_RUN_OVERWRITE": "invalid"}
        config = load_config(cli={"llm": {"batch_size": 2}, "run": {"overwrite": False}},
                             environ=invalid_env)
        self.assertEqual(config.llm.batch_size, 2)
        self.assertFalse(config.overwrite)
        with self.assertRaises(ConfigurationError):
            load_config(cli={"llm": {"batch_size": 2}}, environ=invalid_env)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "providers.toml"
            filename.write_text('[llm]\nbatch_size=3\n[run]\noverwrite=true\n')
            config = load_config(filename, environ=invalid_env)
            self.assertEqual(config.llm.batch_size, 3)
            self.assertTrue(config.overwrite)

    def test_media_context_is_opt_in_and_strictly_boolean(self):
        self.assertFalse(load_config(environ={}).include_media_context)
        self.assertTrue(load_config(cli={"run": {"include_media_context": True}}, environ={}).include_media_context)
        self.assertTrue(load_config(environ={"MEDIA_TO_SUBS_RUN_INCLUDE_MEDIA_CONTEXT": "true"}).include_media_context)
        with self.assertRaises(ConfigurationError):
            load_config(cli={"run": {"include_media_context": "true"}}, environ={})

    def test_separate_environment_keys_are_redacted(self):
        config = load_config(environ={"MEDIA_TO_SUBS_STT_API_KEY": "synthetic-stt-key",
                                     "MEDIA_TO_SUBS_LLM_API_KEY": "synthetic-llm-key"})
        self.assertEqual(config.stt.api_key, "synthetic-stt-key")
        self.assertEqual(config.llm.api_key, "synthetic-llm-key")
        self.assertNotIn("synthetic-", repr(config))
        for section in ("stt", "llm"):
            with self.assertRaises(ConfigurationError) as raised:
                load_config(cli={section: {"api_key": "do-not-echo"}}, environ={})
            self.assertNotIn("do-not-echo", str(raised.exception))

    def test_no_implicit_file_or_other_application_configuration_reads(self):
        with patch("builtins.open", side_effect=AssertionError("Unexpected file read")), \
             patch.object(Path, "open", side_effect=AssertionError("Unexpected path read")):
            config = load_config(environ={"OPENAI_API_KEY": "unused", "WHISPER_MODEL": "unused"})
        self.assertIsNone(config.stt.api_key)
        self.assertIsNone(config.stt.model_path)

    def test_reject_unknown_or_malformed_configuration_without_echoing_values(self):
        invalid = [{"unknown": {}}, {"stt": {"wat": 3}}, {"stt": []},
                   {"stt": {"cpu_threads": True}}, {"llm": {"batch_size": 0}},
                   {"run": {"overwrite": "false"}}, {"stt": {"mode": "invalid"}},
                   {"stt": {"chunk_duration": 10, "chunk_overlap": 10}},
                   {"stt": {"source_language": "unsupported"}},
                   {"stt": {"audio_stream": -1}}, {"stt": {"max_upload_bytes": 0}},
                   {"stt": {"model": 12}}, {"llm": {"mode": "api", "managed_server": True}}]
        for cli in invalid:
            with self.subTest(cli=cli), self.assertRaises(ConfigurationError):
                load_config(cli=cli, environ={})
        for value in (0, -1, math.nan, math.inf, True, "10"):
            with self.subTest(timeout=value), self.assertRaises(ConfigurationError):
                STTConfig(read_timeout=value)
        with self.assertRaises(ConfigurationError):
            load_config(environ={"MEDIA_TO_SUBS_RUN_OVERWRITE": "maybe"})

    def test_reject_invalid_toml_and_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            for contents in ('[stt\nkey="private-marker"', '[stt]\napi_key="private-marker"',
                             '[llm]\nunknown="private-marker"'):
                path.write_text(contents)
                with self.assertRaises(ConfigurationError) as raised:
                    load_config(path, environ={})
                self.assertNotIn("private-marker", str(raised.exception))
            with self.assertRaises(ConfigurationError):
                load_config(Path(directory) / "missing.toml", environ={})

    def test_unsafe_urls_rejected_without_secret_echo(self):
        invalid = ["https://user:secret@example.com/v1", "file:///tmp/model", "https://example.com?key=secret",
                   "https://example.com/#secret", "https://example.com:bad/v1", "https://example.com:0",
                   "https://exa mple.com", "https://example.com\n", "https://example.com\\@evil.org",
                   "https://", "//example.com", "https://%31%32%37.0.0.1", "https://[::1"]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ConfigurationError) as raised:
                LLMConfig(mode="api", url=url)
            self.assertNotIn("secret", str(raised.exception))
        for url in ("http://localhost:8080/v1", "http://127.0.0.2:8000", "http://[::1]:9000/v1"):
            self.assertEqual(LLMConfig(url=url).url, url)
        for url in ("https://example.com/v1", "http://localhost.example.com", "http://192.168.1.2"):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                LLMConfig(url=url)
        self.assertEqual(LLMConfig(mode="api", url="https://example.com/v1").url, "https://example.com/v1")


if __name__ == "__main__":
    unittest.main()
