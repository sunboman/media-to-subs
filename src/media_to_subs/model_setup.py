#!/usr/bin/env python3
"""Explicit, standard-library model setup for macOS/Linux; never an install hook.

Only verified bytes become a final model. Interrupted transfers keep a .part file;
rerunning requests the remaining bytes. A persistent .lock file uses an OS lock,
so crashes release ownership without deleting another process's lock inode.
"""
import argparse
from contextlib import contextmanager
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:
    fcntl = None

CHUNK = 1024 * 1024
DEFAULT_MANIFEST = Path(__file__).with_name("models-manifest.json")


class SetupError(Exception):
    """A concise setup failure which must not expose remote response bodies."""


def safe_url(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback))):
        raise SetupError("Model URLs require HTTPS without credentials (HTTP only on loopback).")
    return parsed


def validate_entry(entry):
    if not isinstance(entry, dict):
        raise SetupError("Invalid model entry.")
    for key in ("filename", "revision", "url", "sha256", "license", "license_url", "source_url"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise SetupError(f"Missing model metadata: {key}.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(bin|gguf)", entry["filename"]):
        raise SetupError("Model filename must be a plain .bin or .gguf filename.")
    if not re.fullmatch(r"[a-f0-9]{40}", entry["revision"]):
        raise SetupError("Model revision must be a full 40-character commit hash.")
    if not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
        raise SetupError("Model SHA256 must be 64 lowercase hex characters.")
    if type(entry.get("bytes")) is not int or entry["bytes"] <= 0:
        raise SetupError("Model byte size must be a positive integer.")
    if entry["revision"] not in safe_url(entry["url"]).path.split("/"):
        raise SetupError("Download URL must contain the pinned revision as a path component.")
    safe_url(entry["source_url"])
    safe_url(entry["license_url"])


def load_manifest(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise SetupError("Unsupported manifest schema.")
        models = data.get("models")
        if not isinstance(models, dict) or not models:
            raise SetupError("Manifest has no models.")
        filenames = set()
        for name, entry in models.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name):
                raise SetupError("Invalid model selection name.")
            validate_entry(entry)
            if entry["filename"] in filenames:
                raise SetupError("Manifest contains duplicate destination filenames.")
            filenames.add(entry["filename"])
        return models
    except (OSError, ValueError) as exc:
        raise SetupError("Cannot read model manifest; check path and JSON syntax.") from exc


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = safe_url(req.full_url)
        new = safe_url(newurl)
        if old.scheme == "https" and new.scheme != "https":
            raise SetupError("Refusing HTTPS downgrade during model download.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@contextmanager
def regular_file(path, flags):
    """Reject symlinks, devices and writable hardlinks before touching content."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise SetupError("Safe model setup currently requires macOS or Linux.")
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SetupError("Model/cache paths must be regular files with one link.")
        with os.fdopen(fd, "rb" if flags == os.O_RDONLY else "r+b") as stream:
            fd = None
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


def verify(stream, entry):
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(CHUNK):
        digest.update(block)
        size += len(block)
    if size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
        raise SetupError("Model size/SHA256 mismatch; file preserved. Move it aside before retrying.")


def download(stream, entry, timeout, max_seconds):
    size = entry["bytes"]
    offset = stream.seek(0, os.SEEK_END)
    if offset > size:
        raise SetupError("Partial file exceeds expected size; move it aside before retrying.")
    if offset == size:
        return
    headers = {"Accept-Encoding": "identity", "User-Agent": "media-to-subs-model-setup/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(entry["url"], headers=headers)
    deadline = time.monotonic() + max_seconds
    opener = urllib.request.build_opener(SafeRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise SetupError("Unexpected encoded model response.")
        if response.status == 206 and offset:
            expected = f"bytes {offset}-{size - 1}/{size}"
            if response.headers.get("Content-Range") != expected:
                raise SetupError("Invalid Content-Range; partial file preserved.")
            remaining = size - offset
        elif response.status == 200:
            if response.headers.get("Content-Range"):
                raise SetupError("Unexpected Content-Range on full response.")
            remaining = size
        else:
            raise SetupError("Unexpected download status; partial file preserved.")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) != remaining):
            raise SetupError("Unexpected Content-Length; partial file preserved.")
        if response.status == 200:  # Range ignored: replace only the owned partial.
            stream.seek(0)
            stream.truncate()
        received = 0
        while True:
            if time.monotonic() > deadline:
                raise SetupError("Download deadline reached; rerun to resume.")
            block = response.read1(min(CHUNK, remaining - received + 1))
            if not block:
                break
            received += len(block)
            if received > remaining:
                raise SetupError("Server sent more bytes than the manifest permits.")
            stream.write(block)
        if received != remaining:
            raise SetupError("Download interrupted; rerun to resume the partial file.")
        stream.flush()
        os.fsync(stream.fileno())


def fetch_model(entry, model_dir, *, timeout=30.0, max_seconds=7200.0):
    validate_entry(entry)
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise SetupError("Timeout must be greater than zero and at most 300 seconds.")
    if not math.isfinite(max_seconds) or not 0 < max_seconds <= 86400:
        raise SetupError("Download deadline must be greater than zero and at most 86400 seconds.")
    if fcntl is None:
        raise SetupError("Safe model setup currently requires macOS or Linux.")
    root = Path(model_dir).expanduser()
    destination = root / entry["filename"]
    partial = root / (entry["filename"] + ".part")
    lock = root / (entry["filename"] + ".lock")
    try:
        root.mkdir(parents=True, exist_ok=True)
        with regular_file(lock, os.O_RDWR | os.O_CREAT) as ownership:
            try:
                fcntl.flock(ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SetupError("Another setup process owns this model; retry when it exits.") from exc
            if os.path.lexists(destination):
                with regular_file(destination, os.O_RDONLY) as cached:
                    verify(cached, entry)
                return destination
            with regular_file(partial, os.O_RDWR | os.O_CREAT) as stream:
                download(stream, entry, timeout, max_seconds)
                verify(stream, entry)
                stream.flush()
                os.fsync(stream.fileno())
                # A hard link publishes complete bytes atomically without clobbering.
                os.link(partial, destination, follow_symlinks=False)
                partial.unlink()
            return destination
    except urllib.error.HTTPError as exc:
        exc.close()
        raise SetupError(f"Model server returned HTTP {exc.code}; partial file preserved.") from exc
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise SetupError("Download/file operation failed; check network, permissions and free space. Files preserved for retry.") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="*", help="Explicit manifest model IDs; no implicit selection")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--list", action="store_true", help="Print pinned metadata without downloading")
    parser.add_argument("--timeout", type=float, default=30, help="Socket timeout in seconds, max 300")
    parser.add_argument("--max-seconds", type=float, default=7200, help="Transfer deadline, max 86400 (plus one socket timeout)")
    args = parser.parse_args(argv)
    try:
        models = load_manifest(args.manifest)
        if args.list:
            print(json.dumps(models, indent=2))
            return 0
        if not args.models:
            raise SetupError("Select model IDs explicitly; use --list to inspect sizes and licenses.")
        if any(name not in models for name in args.models):
            raise SetupError("Unknown model selection; use --list for available IDs.")
        for name in dict.fromkeys(args.models):
            print(f"Verified: {fetch_model(models[name], args.model_dir, timeout=args.timeout, max_seconds=args.max_seconds)}")
        return 0
    except SetupError as exc:
        print(f"Model setup: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Model setup interrupted; rerun to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
