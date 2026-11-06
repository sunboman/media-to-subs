"""Observable archive checks using small, synthetic distribution pairs."""
import importlib.util
import io
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_distribution.py"
SPEC = importlib.util.spec_from_file_location("check_distribution", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

DIST = "media_to_subs-0.1.0"
INFO = DIST + ".dist-info/"
METADATA = (b"Metadata-Version: 2.4\nName: media-to-subs\nVersion: 0.1.0\n"
            b"Requires-Python: >=3.11\nLicense-Expression: MIT AND CC-BY-4.0\n"
            b"License-File: LICENSE\nLicense-File: LICENSES/CC-BY-4.0.txt\n"
            b"License-File: examples/README.md\n\n")
LICENSES = {name: (ROOT / name).read_bytes() for name in
            ("LICENSE", "LICENSES/CC-BY-4.0.txt", "examples/README.md")}
MANIFEST = b'{"schema_version": 1, "models": {"synthetic": {}}}\n'


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.wheel = self.directory / (DIST + "-py3-none-any.whl")
        self.sdist = self.directory / (DIST + ".tar.gz")
        package = {
            "__init__.py": b'__version__ = "0.1.0"\n',
            "__main__.py": b"from .cli import main\nmain()\n",
            "cli.py": b"def main(): pass\n",
            "model_setup.py": b"# Synthetic downloader module.\n",
            "profile.py": b"# Source code is not a saved profile.\n",
            "models-manifest.json": MANIFEST,
        }
        self.wheel_files = {"media_to_subs/" + k: v for k, v in package.items()}
        self.wheel_files.update({
            INFO + "METADATA": METADATA,
            INFO + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            INFO + "RECORD": b"synthetic record\n",
            INFO + "entry_points.txt": b"[console_scripts]\nmedia-to-subs = media_to_subs.cli:main\n",
        })
        self.wheel_files.update({INFO + "licenses/" + name: data
                                 for name, data in LICENSES.items()})
        self.sdist_files = {"src/media_to_subs/" + k: v for k, v in package.items()}
        self.sdist_files.update({
            "PKG-INFO": METADATA,
            "pyproject.toml": (
                b'[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
                b'[project]\nname = "media-to-subs"\nversion = "0.1.0"\n'
                b'requires-python = ">=3.11"\nlicense = "MIT AND CC-BY-4.0"\n'
                b'license-files = ["LICENSE", "LICENSES/CC-BY-4.0.txt", "examples/README.md"]\n'
                b'[project.scripts]\n'
                b'media-to-subs = "media_to_subs.cli:main"\n'),
            "README.md": b"Synthetic package documentation.\n",
            "models-manifest.json": MANIFEST,
            "examples/local-local.toml": b'stt_backend = "local"\n',
        })
        self.sdist_files.update(LICENSES)

    def write_pair(self):
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, data in self.wheel_files.items():
                archive.writestr(name, data)
        with tarfile.open(self.sdist, "w:gz") as archive:
            for name, data in self.sdist_files.items():
                member = tarfile.TarInfo(DIST + "/" + name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def check(self):
        checker.check_distribution(self.wheel, self.sdist)

    def test_minimal_licensed_pair_passes(self):
        self.write_pair()
        self.check()

    def test_license_metadata_and_files_are_required_and_consistent(self):
        for files, name in ((self.wheel_files, INFO + "licenses/LICENSE"),
                            (self.wheel_files, INFO + "licenses/LICENSES/CC-BY-4.0.txt"),
                            (self.sdist_files, "LICENSE"),
                            (self.sdist_files, "LICENSES/CC-BY-4.0.txt")):
            with self.subTest(missing=name):
                original = files.pop(name)
                self.write_pair()
                with self.assertRaises(checker.CheckError):
                    self.check()
                files[name] = original
        for files, name, bad in (
            (self.wheel_files, INFO + "METADATA", METADATA.replace(b"License-Expression: MIT AND CC-BY-4.0\n", b"")),
            (self.sdist_files, "PKG-INFO", METADATA.replace(b"MIT AND CC-BY-4.0", b"MIT")),
            (self.wheel_files, INFO + "licenses/LICENSE", b"wrong license text"),
        ):
            with self.subTest(invalid=name):
                original = files[name]
                files[name] = bad
                self.write_pair()
                with self.assertRaises(checker.CheckError):
                    self.check()
                files[name] = original

    def test_unsafe_paths_and_private_artifacts_are_rejected_in_both_formats(self):
        paths = (
            "../escape", "/absolute", "C:/escape", "dir\\escape", "dir/./file",
            "dir//file", "dir/../file", ".env", ".env.example", "nested/.env.local",
            "credentials.json", "credentials.toml", ".aws/credentials", ".ssh/id_rsa",
            "profile.json", "profile.json.bak", ".profile-backup", "profiles/user.toml",
            "models/weights", "weights.gguf", "weights.bin", "weights.safetensors",
            ".cache/data", "cache/data", "__pycache__/module.pyc", "build/output",
            "dist/output.whl", ".venv/pyvenv.cfg", "venv/bin/python", ".git/config",
            "package.egg-info/PKG-INFO", "private/data", "work/notes", "episode.mp4",
            "audio.wav", "subtitle.srt", "episode.transcript.json",
        )
        for kind in ("wheel", "sdist"):
            files = self.wheel_files if kind == "wheel" else self.sdist_files
            for name in paths:
                with self.subTest(kind=kind, name=name):
                    files[name] = b"synthetic forbidden payload"
                    self.write_pair()
                    with self.assertRaises(checker.CheckError):
                        self.check()
                    del files[name]

    def test_missing_metadata_entrypoint_and_resources_are_rejected(self):
        for files, names in (
            (self.wheel_files, (INFO + "METADATA", INFO + "WHEEL", INFO + "RECORD",
                                INFO + "entry_points.txt", "media_to_subs/cli.py",
                                "media_to_subs/__main__.py", "media_to_subs/models-manifest.json")),
            (self.sdist_files, ("PKG-INFO", "pyproject.toml", "models-manifest.json",
                                "src/media_to_subs/model_setup.py")),
        ):
            for name in names:
                with self.subTest(name=name):
                    data = files.pop(name)
                    self.write_pair()
                    with self.assertRaises(checker.CheckError):
                        self.check()
                    files[name] = data

    def test_invalid_metadata_entrypoint_and_manifest_are_rejected(self):
        mutations = (
            (self.wheel_files, INFO + "METADATA", METADATA.replace(b"media-to-subs", b"other")),
            (self.wheel_files, INFO + "entry_points.txt", b"[console_scripts]\nmedia-to-subs = other:main\n"),
            (self.wheel_files, INFO + "WHEEL", b"not wheel metadata"),
            (self.sdist_files, "PKG-INFO", METADATA.replace(b"0.1.0", b"0.2.0")),
            (self.sdist_files, "pyproject.toml", b"[broken"),
            (self.sdist_files, "pyproject.toml", b'[project]\nname = "media-to-subs"\n'),
        )
        for files, name, replacement in mutations:
            with self.subTest(name=name):
                original = files[name]
                files[name] = replacement
                self.write_pair()
                with self.assertRaises(checker.CheckError):
                    self.check()
                files[name] = original
        for files, name in ((self.wheel_files, "media_to_subs/models-manifest.json"),
                            (self.sdist_files, "src/media_to_subs/models-manifest.json"),
                            (self.sdist_files, "models-manifest.json")):
            files[name] = b"not JSON"
        self.write_pair()
        with self.assertRaises(checker.CheckError):
            self.check()

    def test_manifest_copy_and_package_file_drift_are_rejected(self):
        for files, name in ((self.sdist_files, "models-manifest.json"),
                            (self.wheel_files, "media_to_subs/models-manifest.json"),
                            (self.wheel_files, "media_to_subs/cli.py")):
            with self.subTest(name=name):
                original = files[name]
                files[name] = original + b"\n"
                self.write_pair()
                with self.assertRaises(checker.CheckError):
                    self.check()
                files[name] = original
        self.sdist_files["src/media_to_subs/extra.py"] = b"# sdist only\n"
        self.write_pair()
        with self.assertRaises(checker.CheckError):
            self.check()

    def test_links_special_members_duplicates_and_collisions_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(tar_type=kind):
                self.write_pair()
                # Rewrite the synthetic sdist with an extra unsafe member.
                with tarfile.open(self.sdist, "r:gz") as archive:
                    members = [(m, archive.extractfile(m).read()) for m in archive]
                with tarfile.open(self.sdist, "w:gz") as archive:
                    for member, data in members:
                        archive.addfile(member, io.BytesIO(data))
                    member = tarfile.TarInfo(DIST + "/link")
                    member.type = kind
                    member.linkname = "../../outside"
                    archive.addfile(member)
                with self.assertRaises(checker.CheckError):
                    self.check()
        for name, mode in (("link", stat.S_IFLNK), ("pipe", stat.S_IFIFO),
                           ("media_to_subs/cli.py", stat.S_IFREG),
                           ("media_to_subs/CLI.py", stat.S_IFREG)):
            with self.subTest(zip_name=name):
                self.write_pair()
                member = zipfile.ZipInfo(name)
                member.create_system = 3
                member.external_attr = (mode | 0o644) << 16
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(self.wheel, "a") as archive:
                        archive.writestr(member, b"synthetic")
                with self.assertRaises(checker.CheckError):
                    self.check()

    def test_only_attributed_existing_fleurs_audio_is_allowed_in_sdist(self):
        # Archive structure is synthetic; this one explicitly approved public asset
        # supplies the real bytes needed to exercise the pinned-hash exception.
        names = ("examples/fleurs-ja-validation-0.wav", "examples/fleurs-fixture.json",
                 "examples/README.md")
        for name in names:
            self.sdist_files[name] = (ROOT / name).read_bytes()
        self.write_pair()
        self.check()
        for name in names:
            with self.subTest(name=name):
                original = self.sdist_files[name]
                self.sdist_files[name] = b"synthetic replacement"
                self.write_pair()
                with self.assertRaises(checker.CheckError):
                    self.check()
                self.sdist_files[name] = original
        self.wheel_files[names[0]] = self.sdist_files[names[0]]
        self.write_pair()
        with self.assertRaises(checker.CheckError):
            self.check()

    def test_file_directory_collision_and_corrupt_zip_payload_are_rejected(self):
        self.write_pair()
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr("media_to_subs/cli.py/empty/", b"")
        with self.assertRaises(checker.CheckError):
            self.check()
        self.write_pair()
        payload = self.wheel.read_bytes().replace(b"def main(): pass\n", b"def main(): fail\n", 1)
        self.wheel.write_bytes(payload)
        with self.assertRaises(checker.CheckError):
            self.check()

    def test_cli_rejects_private_contents_without_echoing_them(self):
        self.sdist_files["profile.json"] = b"SYNTHETIC_PROFILE_CONTENT_DO_NOT_ECHO"
        self.write_pair()
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--wheel", str(self.wheel),
             "--sdist", str(self.sdist)], cwd=self.directory, capture_output=True,
            text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distribution check failed", result.stderr)
        self.assertNotIn("SYNTHETIC_PROFILE_CONTENT_DO_NOT_ECHO", result.stdout + result.stderr)

    def test_nontext_metadata_headers_fail_without_traceback(self):
        command = [sys.executable, "-I", str(SCRIPT), "--wheel", str(self.wheel),
                   "--sdist", str(self.sdist)]
        for files, name in ((self.wheel_files, INFO + "METADATA"),
                            (self.sdist_files, "PKG-INFO")):
            for field, value in ((b"Name", b"media-to-subs"),
                                 (b"Requires-Python", b">=3.11")):
                with self.subTest(name=name, field=field):
                    files[name] = METADATA.replace(field + b": " + value,
                                                   field + b": \xff")
                    self.write_pair()
                    result = subprocess.run(command, cwd=self.directory,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("distribution check failed", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    files[name] = METADATA

    def test_malformed_archives_and_cli_exit_status(self):
        self.write_pair()
        command = [sys.executable, "-I", str(SCRIPT), "--wheel", str(self.wheel),
                   "--sdist", str(self.sdist)]
        result = subprocess.run(command, cwd=self.directory, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (self.wheel, self.sdist):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(b"synthetic malformed archive")
                with self.assertRaises(checker.CheckError):
                    self.check()
                result = subprocess.run(command, cwd=self.directory, capture_output=True,
                                        text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("distribution check failed", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
