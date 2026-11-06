"""Private saved settings and endpoint-bound credentials, using isolated roots."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_to_subs.config import load_config
from media_to_subs.errors import ConfigurationError
from media_to_subs.profile import Profile, bind_credentials, load_profile, profile_path, save_profile


def api_profile():
    return Profile({"stt": {"mode": "api", "url": "https://stt.invalid/v1", "model": "speech"},
                    "llm": {"mode": "api", "url": "https://llm.invalid/v1", "model": "translate"}},
                   {"stt": {"mode": "api", "url": "https://stt.invalid/v1/audio/transcriptions", "key": "fake-stt"},
                    "llm": {"mode": "api", "url": "https://llm.invalid/v1/chat/completions", "key": "fake-llm"}})


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "config" / "media-to-subs" / "profile.json"

    def test_path_is_intentional_user_location(self):
        self.assertEqual(profile_path({"HOME": str(self.root)}), self.root / ".config/media-to-subs/profile.json")
        self.assertEqual(profile_path({"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "cfg")}),
                         self.root / "cfg/media-to-subs/profile.json")
        with self.assertRaises(ConfigurationError):
            profile_path({"XDG_CONFIG_HOME": "relative"})

    def test_private_atomic_round_trip_and_redacted_repr(self):
        original = api_profile()
        save_profile(original, self.path)
        loaded = load_profile(self.path)
        self.assertEqual(loaded, original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("fake-", repr(loaded))
        self.assertIsNone(load_profile(self.root / "absent/profile.json"))

    def test_credentials_match_request_endpoint_not_just_origin(self):
        saved = api_profile()
        config = load_config(defaults=saved.settings, environ={})
        self.assertEqual(bind_credentials(config, saved, {}).stt.api_key, "fake-stt")
        equivalent = replace(config.stt, url="https://STT.INVALID:443/v1/audio/transcriptions/")
        self.assertEqual(bind_credentials(replace(config, stt=equivalent), saved, {}).stt.api_key, "fake-stt")
        for url in ("https://evil.invalid/v1", "https://stt.invalid/other", "http://stt.invalid/v1"):
            changed = replace(config, stt=replace(config.stt, url=url))
            with self.subTest(url=url), self.assertRaises(ConfigurationError) as raised:
                bind_credentials(changed, saved, {})
            self.assertIn("setup", str(raised.exception))
            self.assertNotIn("fake-", str(raised.exception))
        changed = replace(config, stt=replace(config.stt, url="https://elsewhere.invalid", api_key="explicit"))
        self.assertEqual(bind_credentials(changed, saved, {"MEDIA_TO_SUBS_STT_API_KEY": "explicit"}).stt.api_key, "explicit")
        local = replace(config, stt=replace(config.stt, mode="local", url=None, api_key="explicit"))
        self.assertIsNone(bind_credentials(local, saved, {"MEDIA_TO_SUBS_STT_API_KEY": "explicit"}).stt.api_key)

    def test_stage_reprs_and_replacements_redact_all_saved_keys(self):
        saved = api_profile()
        saved.settings["llm"]["model"] = "fake-stt"
        saved.settings["stt"]["model"] = "fake-llm"
        config = bind_credentials(load_config(defaults=saved.settings, environ={}), saved, {})
        local = replace(config, stt=replace(config.stt, mode="local", url=None, api_key=None),
                        llm=replace(config.llm, mode="local", url=None, api_key=None))
        for value in (config, config.stt, config.llm, replace(config.llm, batch_size=3), local):
            self.assertNotIn("fake-", repr(value))

    def test_malformed_unknown_partial_and_oversized_state_fails_closed(self):
        save_profile(api_profile(), self.path)
        valid = json.loads(self.path.read_text())
        variants = [{}, {**valid, "version": True}, {**valid, "extra": 1},
                    {**valid, "settings": {"stt": {"mode": "api"}}},
                    {**valid, "settings": {**valid["settings"], "run": {"overwrite": True}}},
                    {**valid, "credentials": {"stt": None}},
                    {**valid, "credentials": {"stt": {"mode": "api", "url": "https://wrong.invalid", "key": "fake-stt"}}},
                    {**valid, "credentials": {"stt": {"mode": "api", "url": 1, "key": []}}}]
        for data in variants:
            self.path.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(ConfigurationError) as raised:
                load_profile(self.path)
            self.assertNotIn("fake-", str(raised.exception))
        for text in ('{"version":1,"version":1}', '[' * 2000, 'x' * 131073):
            self.path.write_text(text)
            with self.assertRaises(ConfigurationError):
                load_profile(self.path)

    def test_reject_links_nonregular_unsafe_modes_and_owner(self):
        save_profile(api_profile(), self.path)
        for mode in (0o644, 0o666, 0o400):
            self.path.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(ConfigurationError):
                load_profile(self.path)
        self.path.chmod(0o600)
        self.path.parent.chmod(0o755)
        with self.assertRaises(ConfigurationError):
            load_profile(self.path)
        self.path.parent.chmod(0o700)
        target = self.path.with_name("target")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(ConfigurationError):
            save_profile(api_profile(), self.path)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(ConfigurationError):
            load_profile(self.path)
        self.path.rmdir()
        target.rename(self.path)
        with patch("media_to_subs.profile.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ConfigurationError):
                load_profile(self.path)

    def test_hardlink_and_parent_symlink_are_rejected(self):
        save_profile(api_profile(), self.path)
        alias = self.path.with_name("alias")
        alias.hardlink_to(self.path)
        with self.assertRaises(ConfigurationError):
            load_profile(self.path)
        alias.unlink()
        link = self.root / "link"
        link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            load_profile(link / "profile.json")

    def test_directory_fsync_failure_rolls_back_after_replace(self):
        save_profile(api_profile(), self.path)
        previous = self.path.read_bytes()
        changed = api_profile()
        changed.settings["stt"]["model"] = "new-model"
        real_fsync = os.fsync
        observed = []
        def fail_directory(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                observed.append(self.path.read_bytes())
                raise OSError("directory sync failed")
            return real_fsync(fd)
        with patch("media_to_subs.profile.os.fsync", side_effect=fail_directory):
            with self.assertRaises(ConfigurationError):
                save_profile(changed, self.path)
        self.assertIn(b"new-model", observed[0])
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_write_and_sync_failure_preserve_old_profile(self):
        save_profile(api_profile(), self.path)
        previous = self.path.read_bytes()
        changed = api_profile()
        changed.settings["stt"]["model"] = "new-model"
        with patch("media_to_subs.profile.os.replace", side_effect=OSError("fake-stt")):
            with self.assertRaises(ConfigurationError):
                save_profile(changed, self.path)
        self.assertEqual(self.path.read_bytes(), previous)
        with patch("media_to_subs.profile.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(ConfigurationError):
                save_profile(changed, self.path)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
