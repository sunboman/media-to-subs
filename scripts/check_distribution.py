#!/usr/bin/env python3
"""Check an explicit Media to Subs wheel/sdist pair without extracting anything.

This is a bounded packaging gate, not an exhaustive secret or license audit.
All archive links and special files are refused: this pure-Python package needs
none. Required source/asset license metadata and notices must be present.
"""
import argparse
import configparser
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tomllib
import zipfile
import zlib


MAX_BYTES = 64 * 1024 * 1024  # Uncompressed archive limit; no bundled models.
MAX_MEMBERS = 5000
FLEURS = "examples/fleurs-ja-validation-0.wav"
FLEURS_SHA256 = "9627b8f5d46806e1a433b443d692fdfb24f26a35c1a47eba26291475fc643d7b"
FORBIDDEN_PARTS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", ".cache",
    "cache", "caches", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build",
    "dist", "models", "model", "weights", "private", "work", "profiles",
    ".ssh", ".aws", ".config", ".local", ".codex", ".opencode", ".agy",
}
FORBIDDEN_SUFFIXES = {
    ".pyc", ".pyo", ".gguf", ".ggml", ".bin", ".safetensors", ".pt", ".pth",
    ".onnx", ".ckpt", ".h5", ".pem", ".key", ".p12", ".pfx",
    ".wav", ".mp3", ".mp4", ".m4a", ".m4v", ".mkv", ".mov", ".webm",
    ".flac", ".ogg", ".opus", ".aac", ".aiff", ".avi", ".ts", ".srt",
    ".vtt", ".ass", ".parquet",
}
REQUIRED_PACKAGE = {
    "__init__.py", "__main__.py", "cli.py", "model_setup.py", "models-manifest.json",
}
LICENSE_EXPRESSION = "MIT AND CC-BY-4.0"
LICENSE_FILES = ("LICENSE", "LICENSES/CC-BY-4.0.txt", "examples/README.md")


class CheckError(ValueError):
    """Distribution did not satisfy the bounded packaging checks."""


def safe_name(name):
    parts = name.removesuffix("/").split("/")
    if (not name or "\\" in name or ":" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(part in ("", ".", "..") for part in parts)):
        raise CheckError("unsafe archive member path")
    return "/".join(parts)


def check_path(name, kind):
    parts = name.lower().split("/")
    for part in parts:
        if (part in FORBIDDEN_PARTS or part.startswith((".env", ".profile-"))
                or part.endswith((".egg-info", ".transcript.json"))
                or part == "transcript.json"
                or part in {".netrc", ".pypirc", ".ds_store"}
                or re.match(r"^(credentials?|secrets?)([._-]|$)", part)
                or re.match(r"^profile(?:\.(?:json|toml|ya?ml|ini|cfg)(?:\.|$)|$)", part)):
            raise CheckError("forbidden private or generated archive member")
    if PurePosixPath(name.lower()).suffix in FORBIDDEN_SUFFIXES:
        if not (kind == "sdist" and name == FLEURS):
            raise CheckError("forbidden model, media, key, or generated archive member")


def archive_files(path, kind):
    """Read bounded regular-file bytes, rejecting ambiguous extraction layouts."""
    files, seen = {}, set()
    total = 0

    def add(name, size, directory, read):
        nonlocal total
        name = safe_name(name)
        if name.casefold() in seen:
            raise CheckError("duplicate or case-colliding archive member")
        seen.add(name.casefold())
        total += size
        if size < 0 or total > MAX_BYTES or len(seen) > MAX_MEMBERS:
            raise CheckError("archive exceeds bounded packaging limits")
        # Strip the single sdist root for path policy checks only.
        relative = name.partition("/")[2] if kind == "sdist" else name
        check_path(relative, kind)
        if not directory:
            data = read()
            if len(data) != size:
                raise CheckError("truncated archive member")
            files[name] = data

    try:
        if kind == "wheel":
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    mode = stat.S_IFMT(member.external_attr >> 16)
                    if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise CheckError("archive links and special members are forbidden")
                    if mode == stat.S_IFDIR and not member.is_dir():
                        raise CheckError("inconsistent archive directory")
                    add(member.orig_filename, member.file_size, member.is_dir(),
                        lambda: archive.read(member))
        else:
            with tarfile.open(path, "r:gz") as archive:
                for member in archive:
                    if not (member.isfile() or member.isdir()) or member.issparse():
                        raise CheckError("archive links and special members are forbidden")
                    add(member.name, member.size, member.isdir(),
                        lambda: archive.extractfile(member).read())
    except CheckError:
        raise
    except (OSError, EOFError, ValueError, RuntimeError, NotImplementedError,
            tarfile.TarError, zipfile.BadZipFile, zlib.error) as exc:
        # Do not echo archive contents or exception text that may contain them.
        raise CheckError(f"malformed or unreadable {kind} archive") from exc
    if not files:
        raise CheckError("empty archive")
    folded = {name.casefold() for name in files}
    for name in seen:
        if any(str(parent) in folded for parent in PurePosixPath(name).parents):
            raise CheckError("archive file/directory collision")
    return files


def require(files, names):
    if any(not files.get(name) for name in names):
        raise CheckError("missing or empty required package metadata/resource")


def metadata(data):
    parsed = BytesParser().parsebytes(data)
    for key in ("Metadata-Version", "Name", "Version", "Requires-Python"):
        values = parsed.get_all(key, [])
        if len(values) != 1 or not isinstance(values[0], str) or not values[0].strip():
            raise CheckError("missing or ambiguous package metadata")
    if any(not isinstance(value, str) for value in parsed.get_all("Requires-Dist", [])):
        raise CheckError("invalid dependency metadata")
    if (re.sub(r"[-_.]+", "-", parsed["Name"]).lower() != "media-to-subs"
            or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", parsed["Version"])):
        raise CheckError("unexpected package name or version")
    return parsed


def check_fleurs(files):
    if FLEURS not in files:
        return
    require(files, {"examples/README.md", "examples/fleurs-fixture.json"})
    if hashlib.sha256(files[FLEURS]).hexdigest() != FLEURS_SHA256:
        raise CheckError("FLEURS audio differs from the attributed existing fixture")
    try:
        record = json.loads(files["examples/fleurs-fixture.json"])
    except (ValueError, UnicodeError) as exc:
        raise CheckError("invalid FLEURS attribution metadata") from exc
    attribution = files["examples/README.md"]
    if (not isinstance(record, dict) or record.get("audio_sha256") != FLEURS_SHA256
            or record.get("license") != "CC-BY-4.0"
            or record.get("source") != "https://huggingface.co/datasets/google/fleurs"
            or b"https://creativecommons.org/licenses/by/4.0/" not in attribution
            or b"https://huggingface.co/datasets/google/fleurs" not in attribution):
        raise CheckError("missing FLEURS source/license attribution")


def check_distribution(wheel, sdist):
    wheel_files = archive_files(wheel, "wheel")
    source_files = archive_files(sdist, "sdist")
    roots = {name.partition("/")[0] for name in source_files}
    if len(roots) != 1 or any("/" not in name for name in source_files):
        raise CheckError("sdist must have exactly one enclosing directory")
    root = roots.pop()
    source_files = {name[len(root) + 1:]: data for name, data in source_files.items()}
    require(source_files, {"PKG-INFO", "pyproject.toml", "README.md", "models-manifest.json"})
    source_meta = metadata(source_files["PKG-INFO"])
    version = source_meta["Version"]
    if root not in {f"media_to_subs-{version}", f"media-to-subs-{version}"}:
        raise CheckError("sdist directory disagrees with package metadata")
    info = f"media_to_subs-{version}.dist-info/"
    require(wheel_files, {info + name for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt")})
    if any(not name.startswith(("media_to_subs/", info)) for name in wheel_files):
        raise CheckError("unexpected wheel package or metadata location")
    wheel_meta = metadata(wheel_files[info + "METADATA"])
    for meta in (source_meta, wheel_meta):
        if (meta.get_all("License-Expression", []) != [LICENSE_EXPRESSION]
                or sorted(meta.get_all("License-File", [])) != sorted(LICENSE_FILES)):
            raise CheckError("missing or inconsistent source/asset license metadata")
    require(source_files, LICENSE_FILES)
    require(wheel_files, {info + "licenses/" + name for name in LICENSE_FILES})
    for name in LICENSE_FILES:
        if source_files[name] != wheel_files[info + "licenses/" + name]:
            raise CheckError("wheel and sdist license texts/notices disagree")
    if (b"MIT License" not in source_files["LICENSE"]
            or b"Permission is hereby granted" not in source_files["LICENSE"]
            or b"THE SOFTWARE IS PROVIDED" not in source_files["LICENSE"]
            or not source_files["LICENSES/CC-BY-4.0.txt"].startswith(
                b"Creative Commons Attribution 4.0 International")):
        raise CheckError("invalid source/asset license text")
    for key in ("Name", "Version", "Requires-Python", "Requires-Dist"):
        if sorted(source_meta.get_all(key, [])) != sorted(wheel_meta.get_all(key, [])):
            raise CheckError("wheel and sdist metadata disagree")
    wheel_tags = BytesParser().parsebytes(wheel_files[info + "WHEEL"])
    if wheel_tags.get("Wheel-Version") != "1.0" or not wheel_tags.get_all("Tag"):
        raise CheckError("invalid wheel metadata")
    try:
        entrypoints = configparser.ConfigParser(interpolation=None)
        entrypoints.read_string(wheel_files[info + "entry_points.txt"].decode("utf-8"))
        project = tomllib.loads(source_files["pyproject.toml"].decode("utf-8"))
        if (entrypoints.get("console_scripts", "media-to-subs") != "media_to_subs.cli:main"
                or project["project"]["scripts"]["media-to-subs"] != "media_to_subs.cli:main"
                or project["project"]["name"] != source_meta["Name"]
                or project["project"]["version"] != version
                or project["project"]["requires-python"] != source_meta["Requires-Python"]
                or project["project"].get("license") != LICENSE_EXPRESSION
                or sorted(project["project"].get("license-files", [])) != sorted(LICENSE_FILES)
                or not project["build-system"]["build-backend"]
                or not project["build-system"]["requires"]):
            raise CheckError("package entrypoint or build metadata disagree")
    except (ValueError, UnicodeError, KeyError, TypeError, configparser.Error) as exc:
        raise CheckError("invalid entrypoint or build metadata") from exc
    wheel_package = {name.removeprefix("media_to_subs/"): data
                     for name, data in wheel_files.items() if name.startswith("media_to_subs/")}
    source_package = {name.removeprefix("src/media_to_subs/"): data
                      for name, data in source_files.items() if name.startswith("src/media_to_subs/")}
    require(wheel_package, REQUIRED_PACKAGE)
    require(source_package, REQUIRED_PACKAGE)
    if wheel_package != source_package:
        raise CheckError("wheel and sdist package contents disagree")
    manifest = source_files["models-manifest.json"]
    if manifest != source_package["models-manifest.json"]:
        raise CheckError("model manifest copies disagree")
    try:
        parsed = json.loads(manifest)
        if (not isinstance(parsed, dict) or parsed.get("schema_version") != 1
                or not isinstance(parsed.get("models"), dict) or not parsed["models"]):
            raise CheckError("invalid model manifest resource")
    except (ValueError, UnicodeError) as exc:
        raise CheckError("invalid model manifest resource") from exc
    check_fleurs(source_files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True, help="explicit .whl path")
    parser.add_argument("--sdist", type=Path, required=True, help="explicit .tar.gz path")
    args = parser.parse_args(argv)
    try:
        check_distribution(args.wheel, args.sdist)
    except CheckError as exc:
        print(f"distribution check failed: {exc}", file=sys.stderr)
        return 1
    print("Distribution checks passed (bounded packaging checks; not an exhaustive audit).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
