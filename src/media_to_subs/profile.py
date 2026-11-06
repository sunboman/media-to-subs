"""Intentional private user state; settings and endpoint-bound keys are one transaction."""
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .config import RunConfig, load_config, validate_url
from .errors import ConfigurationError

_MAX_BYTES = 128 * 1024
_ERROR = "Cannot use saved profile; check its structure, ownership and private permissions (directory 0700, file 0600)"


@dataclass(frozen=True)
class Profile:
    settings: dict = field(repr=False)
    credentials: dict = field(default_factory=dict, repr=False)

    def secrets(self) -> tuple[str, ...]:
        return tuple(item["key"] for item in self.credentials.values())


def profile_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    root = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    if not root.is_absolute():
        raise ConfigurationError("HOME and XDG_CONFIG_HOME must select an absolute user configuration directory")
    return root / "media-to-subs" / "profile.json"


def request_url(stage: str, url: str) -> str:
    """Normalize only origin spelling and the adapter's actual appended suffix."""
    validate_url(url)
    parts = urlsplit(url.rstrip("/"))
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        host += f":{port}"
    suffix = "/audio/transcriptions" if stage == "stt" else "/chat/completions"
    path = parts.path if parts.path.endswith(suffix) else parts.path + suffix
    return urlunsplit((parts.scheme, host, path, "", ""))


def _validate(profile: Profile) -> None:
    settings, credentials = profile.settings, profile.credentials
    if (not isinstance(settings, dict) or not {"stt", "llm"} <= settings.keys()
            or not isinstance(credentials, dict) or set(credentials) - {"stt", "llm"}):
        raise ConfigurationError(_ERROR)
    if (not isinstance(settings.get("run", {}), dict)
            or set(settings.get("run", {})) - {"glossary", "include_media_context"}):
        raise ConfigurationError(_ERROR)
    config = load_config(defaults=settings, environ={})
    for stage in ("stt", "llm"):
        options = settings[stage]
        selected = getattr(config, stage)
        if "mode" not in options:
            raise ConfigurationError(_ERROR)
        if selected.mode == "api":
            if not options.get("url") or not options.get("model"):
                raise ConfigurationError(_ERROR)
        elif selected.mode == "agent":
            if any(options.get(name) for name in ("url", "model", "model_path", "managed_server")):
                raise ConfigurationError(_ERROR)
        elif stage == "stt":
            if not selected.model_path or not selected.vad_model:
                raise ConfigurationError(_ERROR)
        elif selected.managed_server:
            if not selected.model_path:
                raise ConfigurationError(_ERROR)
        elif not selected.url or not selected.model:
            raise ConfigurationError(_ERROR)
        for name in ("model_path", "vad_model"):
            value = options.get(name)
            if value is not None and (not isinstance(value, str) or not Path(value).is_absolute()):
                raise ConfigurationError(_ERROR)
        item = credentials.get(stage)
        if stage in credentials:
            if (not isinstance(item, dict) or set(item) != {"mode", "url", "key"}
                    or item["mode"] != "api" or selected.mode != "api"
                    or not isinstance(item["key"], str) or not item["key"].strip()
                    or any(ord(c) < 33 or ord(c) == 127 for c in item["key"])
                    or item["url"] != request_url(stage, selected.url)):
                raise ConfigurationError(_ERROR)
    glossary = settings.get("run", {}).get("glossary")
    if glossary is not None and (not isinstance(glossary, str) or not Path(glossary).is_absolute()):
        raise ConfigurationError(_ERROR)


def bind_credentials(config: RunConfig, profile: Profile, environ: Mapping[str, str]) -> RunConfig:
    """Never transfer a saved key to another request URL, mode, or stage."""
    selected = {}
    secrets = (profile.secrets() + tuple(key for key in (config.stt.api_key, config.llm.api_key) if key)
               + config.stt._redaction_secrets + config.llm._redaction_secrets)
    for stage in ("stt", "llm"):
        options = getattr(config, stage)
        if options.mode != "api":
            selected[stage] = replace(options, api_key=None, _redaction_secrets=secrets)
            continue
        key = options.api_key
        saved = profile.credentials.get(stage)
        if f"MEDIA_TO_SUBS_{stage.upper()}_API_KEY" not in environ and saved:
            if options.url is None or request_url(stage, options.url) != saved["url"] or options.mode != saved["mode"]:
                raise ConfigurationError(f"Saved {stage.upper()} credential destination changed; run media-to-subs setup or supply an explicit MEDIA_TO_SUBS_{stage.upper()}_API_KEY")
            key = saved["key"]
        selected[stage] = replace(options, api_key=key, _redaction_secrets=secrets)
    return replace(config, **selected)


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Walk without following links, holding an fd to the private leaf directory."""
    path = path.absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            if part in (".", ".."):
                raise ConfigurationError(_ERROR)
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            leaf = index == len(path.parts) - 2
            if leaf:
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ConfigurationError(_ERROR)
            elif (info.st_uid not in (0, os.getuid())
                  or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                raise ConfigurationError(_ERROR)
        yield fd
    finally:
        os.close(fd)


def _read(fd: int, name: str) -> bytes | None:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > _MAX_BYTES):
            raise ConfigurationError(_ERROR)
        data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ConfigurationError(_ERROR)
        return data


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _decode(data: bytes) -> Profile:
    document = json.loads(data, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if (not isinstance(document, dict) or set(document) != {"version", "settings", "credentials"}
            or type(document["version"]) is not int or document["version"] != 1):
        raise ConfigurationError(_ERROR)
    profile = Profile(document["settings"], document["credentials"])
    _validate(profile)
    return profile


def load_profile(path: Path | None = None) -> Profile | None:
    path = profile_path() if path is None else path
    try:
        with _directory(path.parent) as fd:
            data = _read(fd, path.name)
        return None if data is None else _decode(data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, RecursionError, ConfigurationError):
        raise ConfigurationError(_ERROR) from None


def _write_temp(fd: int, name: str, data: bytes) -> None:
    file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(file_fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save_profile(profile: Profile, path: Path | None = None, *, cancellation=None) -> None:
    """Publish a validated private file; rollback if directory durability fails."""
    path = profile_path() if path is None else path
    try:
        _validate(profile)
        data = (json.dumps({"version": 1, "settings": profile.settings, "credentials": profile.credentials},
                           ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()
        if len(data) > _MAX_BYTES:
            raise ConfigurationError(_ERROR)
        with _directory(path.parent, create=True) as fd:
            old = _read(fd, path.name)
            if old is not None:
                _decode(old)
            temporary, backup = ".profile-" + uuid4().hex, ".profile-" + uuid4().hex
            replaced = False
            try:
                _write_temp(fd, temporary, data)
                if old is not None:
                    _write_temp(fd, backup, old)
                # Recheck the original immediately before publishing.
                if _read(fd, path.name) != old:
                    raise ConfigurationError("Saved profile changed during setup; rerun setup")
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                os.replace(temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd)
                replaced = True
                os.fsync(fd)
            except BaseException:
                if replaced:
                    if old is None:
                        os.unlink(path.name, dir_fd=fd)
                    else:
                        os.replace(backup, path.name, src_dir_fd=fd, dst_dir_fd=fd)
                raise
            finally:
                for name in (temporary, backup):
                    try:
                        os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
    except (OSError, ValueError, TypeError, RecursionError, ConfigurationError):
        raise ConfigurationError(_ERROR) from None
