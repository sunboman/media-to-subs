"""The installed downloader and checkout command share code and pinned metadata."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ModelPackagingTests(unittest.TestCase):
    def test_packaged_manifest_matches_repository_copy(self):
        from media_to_subs import model_setup

        self.assertEqual(model_setup.DEFAULT_MANIFEST.read_bytes(),
                         (ROOT / "models-manifest.json").read_bytes())
        self.assertEqual(len(model_setup.load_manifest(model_setup.DEFAULT_MANIFEST)), 3)

    def test_checkout_wrapper_works_outside_checkout_without_installed_package(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"PATH": "/usr/bin:/bin", "HOME": home, "XDG_DATA_HOME": home}
            result = subprocess.run(
                [sys.executable, "-I", str(ROOT / "scripts/fetch_models.py"), "--list"],
                cwd=home, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)), 3)
            self.assertEqual(os.listdir(home), [])
