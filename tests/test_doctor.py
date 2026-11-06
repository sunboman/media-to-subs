from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_to_subs.config import LLMConfig, RunConfig, STTConfig
from media_to_subs.doctor import check_config


class DoctorTests(unittest.TestCase):
    def test_managed_llm_requires_supported_http_route(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model"
            model.write_bytes(b"fixture")
            config = RunConfig(stt=STTConfig(mode="api", url="https://stt.invalid", model="speech"),
                               llm=LLMConfig(managed_server=True, model_path=model, url="https://localhost:8080/wrong"))
            with patch("media_to_subs.doctor.shutil.which", return_value="/bin/true"):
                self.assertFalse(all(check.ok for check in check_config(config)))

    def test_advanced_local_config_can_omit_optional_vad(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model"
            model.write_bytes(b"fixture")
            config = RunConfig(stt=STTConfig(model_path=model),
                               llm=LLMConfig(url="http://localhost:8080/v1", model="local"))
            with patch("media_to_subs.doctor.shutil.which", return_value="/bin/true"):
                checks = check_config(config)
            self.assertTrue(all(check.ok for check in checks), checks)
            self.assertFalse(any(check.label == "VAD model" for check in checks))

    def test_api_stages_only_require_media_tools_and_never_print_options(self):
        config = RunConfig(stt=STTConfig(mode="api", url="https://stt.invalid", model="fake-secret", api_key="fake-secret"),
                           llm=LLMConfig(mode="api", url="https://llm.invalid", model="fake-secret", api_key="fake-secret"))
        with patch("media_to_subs.doctor.shutil.which", side_effect=lambda binary: "/bin/true" if binary in ("ffmpeg", "ffprobe") else None):
            checks = check_config(config)
        self.assertTrue(all(check.ok for check in checks), checks)
        self.assertNotIn("fake-secret", repr(checks))
        self.assertFalse(any("whisper" in check.label or "llama" in check.label for check in checks))

    def test_local_modes_check_selected_assets_and_offer_official_install_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model"
            model.write_bytes(b"fixture")
            config = RunConfig(stt=STTConfig(model_path=model, vad_model=model),
                               llm=LLMConfig(managed_server=True, model_path=model))
            with patch("media_to_subs.doctor.shutil.which", return_value=None):
                checks = check_config(config)
            messages = " ".join(check.detail for check in checks)
            for formula in ("ffmpeg", "whisper.cpp", "llama.cpp"):
                self.assertIn("brew install " + formula, messages)
                self.assertIn("https://formulae.brew.sh/formula/" + formula, messages)
            self.assertTrue(all(check.ok for check in checks if "model" in check.label.lower()))
            existing = replace(config, llm=LLMConfig(url="http://localhost:8080/v1", model="local"))
            with patch("media_to_subs.doctor.shutil.which", return_value="/bin/true"):
                self.assertTrue(all(check.ok for check in check_config(existing)))
            model.unlink()
            with patch("media_to_subs.doctor.shutil.which", return_value="/bin/true"):
                self.assertFalse(all(check.ok for check in check_config(config)))


if __name__ == "__main__":
    unittest.main()
