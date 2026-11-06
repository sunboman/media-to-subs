"""Installer transactions use isolated user roots; only one test creates a venv."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="installer test ")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.data = self.home / "data directory"
        self.bin = self.home / "bin directory"
        self.command = self.bin / "media-to-subs"
        self.root = self.data / "media-to-subs/app"
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                    "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.home / "config")}
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def fake_build(self, destination, source, **kwargs):
        (destination / "bin").mkdir()
        command = destination / "bin/media-to-subs"
        command.write_text("#!/bin/sh\nprintf 'working\\n'\n")
        command.chmod(0o700)

    def install(self, build=None):
        with patch.object(installer, "build_environment", side_effect=build or self.fake_build), \
             patch.object(installer, "verify_environment", return_value="0.1.0"):
            return installer.install(ROOT, self.data, self.bin)

    def test_install_and_repeat_switch_only_after_new_environment_ready(self):
        self.install()
        previous = self.command.resolve()

        def building(destination, source, **kwargs):
            self.assertEqual(self.command.resolve(), previous)
            self.assertTrue(previous.is_file())
            self.fake_build(destination, source)

        self.install(building)
        self.assertNotEqual(self.command.resolve(), previous)
        self.assertTrue(previous.is_file())
        self.assertEqual(subprocess.check_output([self.command], text=True), "working\n")

    def test_build_failure_and_verification_failure_preserve_previous_install(self):
        self.install()
        previous = os.readlink(self.command)
        installs = set((self.root / "installs").iterdir())
        for stage in ("build_environment", "verify_environment"):
            with self.subTest(stage=stage), \
                 patch.object(installer, "build_environment", side_effect=self.fake_build), \
                 patch.object(installer, "verify_environment", return_value="0.1.0"), \
                 patch.object(installer, stage, side_effect=installer.InstallError("failed")):
                with self.assertRaises(installer.InstallError):
                    installer.install(ROOT, self.data, self.bin)
            self.assertEqual(os.readlink(self.command), previous)
            self.assertEqual(set((self.root / "installs").iterdir()), installs)

    def test_unrelated_existing_command_or_symlink_survives(self):
        self.bin.mkdir()
        other = self.home / "other"
        other.write_text("user file")
        for kind in ("regular", "symlink", "broken"):
            with self.subTest(kind=kind):
                if kind == "regular":
                    self.command.write_text("user file")
                else:
                    self.command.symlink_to(other if kind == "symlink" else self.home / "missing")
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertTrue(os.path.lexists(self.command))
                self.assertEqual(other.read_text(), "user file")
                if kind == "regular":
                    self.assertEqual(self.command.read_text(), "user file")
                self.command.unlink()

    def test_unmarked_existing_data_is_never_claimed(self):
        self.root.mkdir(parents=True, mode=0o700)
        marker = self.root / "user-file"
        marker.write_text("keep")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(set(self.root.iterdir()), {marker})

    def test_model_cache_from_source_setup_survives_install_and_uninstall(self):
        models = self.data / "media-to-subs/models"
        models.mkdir(parents=True)
        existing = models / "existing.bin"
        existing.write_bytes(b"existing model")
        self.install()
        self.assertIn("/media-to-subs/app/installs/", str(self.command.resolve()))
        installer.uninstall(self.data, self.bin)
        self.assertEqual(existing.read_bytes(), b"existing model")

    def test_interruption_after_switch_does_not_delete_the_published_environment(self):
        self.install()
        original = installer.publish
        def interrupt_after_switch(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(installer, "publish", side_effect=interrupt_after_switch):
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        self.assertTrue(self.command.exists())
        self.assertEqual(subprocess.check_output([self.command], text=True), "working\n")

    def test_symlinked_directory_and_writable_directory_are_rejected(self):
        actual = self.home / "actual"
        actual.mkdir()
        self.bin.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(list(actual.iterdir()), [])
        self.bin.unlink()
        self.bin.mkdir(mode=0o777)
        self.bin.chmod(0o777)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(list(self.bin.iterdir()), [])

    def test_lock_symlink_and_hardlink_are_not_modified(self):
        self.bin.mkdir()
        other = self.home / "other"
        other.write_text("keep")
        lock = self.bin / ".media-to-subs.install.lock"
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    lock.symlink_to(other)
                else:
                    lock.hardlink_to(other)
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertEqual(other.read_text(), "keep")
                lock.unlink()

    def test_competing_install_cannot_start_build(self):
        def build(destination, source, **kwargs):
            with self.assertRaisesRegex(installer.InstallError, "Another installer"):
                self.install()
            self.fake_build(destination, source)
        self.install(build)

    def test_same_data_root_with_different_bin_directory_is_still_locked(self):
        def build(destination, source, **kwargs):
            with self.assertRaisesRegex(installer.InstallError, "Another installer"):
                installer.install(ROOT, self.data, self.home / "another bin")
            self.fake_build(destination, source)
        self.install(build)

    def test_data_root_symlink_cannot_redirect_installs_or_uninstall(self):
        target = self.home / "unrelated directory"
        target.mkdir()
        (target / "keep").write_text("keep")
        self.data.mkdir()
        (self.data / "media-to-subs").symlink_to(target)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(list(target.iterdir()), [target / "keep"])

    def test_uninstall_refuses_replacement_command(self):
        self.install()
        owned = self.command.resolve()
        self.command.unlink()
        self.command.write_text("replacement")
        with self.assertRaises(installer.InstallError):
            installer.uninstall(self.data, self.bin)
        self.assertEqual(self.command.read_text(), "replacement")
        self.assertTrue(owned.is_file())

    def test_subprocess_failure_suppresses_logs_and_profile_keys(self):
        failure = subprocess.CalledProcessError(1, ["pip"], output="secret in URL", stderr="secret")
        with patch.dict(os.environ, {"MEDIA_TO_SUBS_STT_API_KEY": "synthetic-key",
                                     "PIP_INDEX_URL": "https://user:secret@example.invalid"}), \
             patch.object(installer.subprocess, "run", side_effect=failure) as child:
            with self.assertRaisesRegex(installer.InstallError, "Build failed") as caught:
                installer.run(["python", "an argument with spaces"], cwd=self.home, message="Build failed")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("MEDIA_TO_SUBS_STT_API_KEY", child.call_args.kwargs["env"])
        self.assertNotIn("PIP_INDEX_URL", child.call_args.kwargs["env"])
        self.assertEqual(child.call_args.args[0], ["python", "an argument with spaces"])

    def test_command_appearing_during_build_is_not_overwritten(self):
        def build(destination, source, **kwargs):
            self.command.write_text("competing command")
            self.fake_build(destination, source)
        with self.assertRaises(installer.InstallError):
            self.install(build)
        self.assertEqual(self.command.read_text(), "competing command")

    def test_corrupt_ownership_and_symlinked_environment_refuse_upgrade(self):
        self.install()
        previous = os.readlink(self.command)
        marker = self.command.resolve().parents[1] / ".install.json"
        record = marker.read_text()
        marker.write_text("{}")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(os.readlink(self.command), previous)
        marker.write_text(record)
        marker.unlink()
        outside = self.home / "outside"
        outside.write_text(record)
        marker.symlink_to(outside)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(outside.read_text(), record)

    def test_uninstall_preserves_profile_models_and_unrelated_files(self):
        self.install()
        self.install()
        models = self.root.parent / "models"
        models.mkdir()
        (models / "keep.bin").write_text("model")
        profile = self.home / "config"
        profile.mkdir()
        (profile / "profile.json").write_text("private profile")
        installer.uninstall(self.data, self.bin)
        self.assertFalse(os.path.lexists(self.command))
        self.assertEqual(list((self.root / "installs").iterdir()), [])
        self.assertEqual((models / "keep.bin").read_text(), "model")
        self.assertEqual((profile / "profile.json").read_text(), "private profile")

    def test_uninstall_rejects_unknown_environment_before_removing_command(self):
        self.install()
        unknown = self.root / "installs/unknown"
        unknown.mkdir()
        (unknown / "keep").write_text("keep")
        with self.assertRaises(installer.InstallError):
            installer.uninstall(self.data, self.bin)
        self.assertTrue(self.command.exists())
        self.assertEqual((unknown / "keep").read_text(), "keep")

    def test_failed_atomic_switch_preserves_old_command(self):
        self.install()
        previous = os.readlink(self.command)
        original = installer.os.replace
        def fail_switch(source, destination, **kwargs):
            if Path(destination) == self.command:
                raise OSError("simulated publication failure")
            return original(source, destination, **kwargs)
        with patch.object(installer.os, "replace", side_effect=fail_switch):
            with self.assertRaises(installer.InstallError):
                self.install()
        self.assertEqual(os.readlink(self.command), previous)
        self.assertTrue(self.command.exists())

    def test_real_offline_venv_install_from_wheel_outside_checkout(self):
        # A tiny local distribution exercises actual pip/venv/console shebangs,
        # without downloading dependencies during every unittest run.
        wheel = self.home / "media_to_subs-0.1.0-py3-none-any.whl"
        metadata = "media_to_subs-0.1.0.dist-info"
        files = {
            "media_to_subs/__init__.py": "__version__ = '0.1.0'\n",
            "media_to_subs/cli.py": "def main():\n    print('media-to-subs 0.1.0 usage: fixture')\n",
            "media_to_subs/model_setup.py": (ROOT / "src/media_to_subs/model_setup.py").read_text(),
            "media_to_subs/models-manifest.json": (ROOT / "models-manifest.json").read_text(),
            f"{metadata}/METADATA": "Metadata-Version: 2.1\nName: media-to-subs\nVersion: 0.1.0\n",
            f"{metadata}/WHEEL": "Wheel-Version: 1.0\nGenerator: unittest\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{metadata}/entry_points.txt": "[console_scripts]\nmedia-to-subs = media_to_subs.cli:main\n",
        }
        files[f"{metadata}/RECORD"] = "".join(f"{name},,\n" for name in files)
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        result = subprocess.run(
            [str(ROOT / "install.sh"), "--source", str(wheel), "--offline", "--bin-dir", str(self.bin)],
            env=self.env | {"MEDIA_TO_SUBS_PYTHON": sys.executable}, cwd=self.home,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = subprocess.check_output([self.command, "--version"], cwd=self.home,
                                         env=self.env, text=True)
        self.assertIn("media-to-subs 0.1.0", output)
        self.assertTrue(self.command.is_symlink())
        self.assertIn("media-to-subs setup\nmedia-to-subs video.mkv", result.stdout)
        self.assertFalse((self.root.parent / "models").exists())
        self.assertFalse((self.home / "config").exists())
