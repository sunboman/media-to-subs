"""Pure configuration: CLI > explicit TOML > namespaced env > saved > defaults."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
import ipaddress
import math
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .errors import ConfigurationError

_PATH_FIELDS = {"model_path", "vad_model", "glossary", "save_source", "output"}
_INT_FIELDS = {"max_retries", "cpu_threads", "max_upload_bytes", "audio_stream", "batch_size"}
_FLOAT_FIELDS = {"connect_timeout", "read_timeout", "startup_timeout", "process_timeout", "agent_timeout",
                 "chunk_duration", "chunk_overlap"}
_BOOL_FIELDS = {"force_transcribe", "managed_server", "overwrite", "include_media_context"}
_OPTIONAL_FIELDS = _PATH_FIELDS | {"url", "model", "api_key", "audio_stream", "agent", "agent_model", "agent_binary"}


def validate_url(url: str, *, local: bool = False) -> None:
    """Validate without DNS resolution or echoing a potentially secret URL."""
    message = "Endpoint must be an HTTP(S) URL without userinfo, query, fragment or unsafe characters"
    if (not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or "\\" in url or "?" in url or "#" in url):
        raise ConfigurationError(message)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigurationError(message) from None
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or "%" in parsed.netloc or port == 0
            or parsed.netloc.endswith(":")):
        raise ConfigurationError(message)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                      for label in host.split(".")):
            raise ConfigurationError(message) from None
    if local and host.lower() != "localhost" and not (address is not None and address.is_loopback):
        raise ConfigurationError("Local endpoint must use localhost or a literal loopback address")


def _validate_fields(config: object) -> None:
    for item in fields(config):
        name = item.name
        value = getattr(config, name)
        if name in {"stt", "llm", "_redaction_secrets"}:
            continue
        if value is None and name in _OPTIONAL_FIELDS:
            continue
        valid = True
        if name in _PATH_FIELDS:
            valid = isinstance(value, (str, Path)) and bool(str(value).strip()) and "\x00" not in str(value)
            if valid:
                object.__setattr__(config, name, Path(value))
        elif name in _BOOL_FIELDS:
            valid = type(value) is bool
        elif name in _INT_FIELDS:
            minimum = 0 if name in {"max_retries", "audio_stream"} else 1
            valid = type(value) is int and value >= minimum
        elif name in _FLOAT_FIELDS:
            try:
                valid = type(value) in (float, int) and math.isfinite(value) and (
                    value >= 0 if name == "chunk_overlap" else value > 0)
            except OverflowError:
                valid = False
        else:
            valid = isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        if not valid:
            raise ConfigurationError(f"Invalid value for {name}")
    if hasattr(config, "mode"):
        modes = {"local", "api", "agent"} if isinstance(config, LLMConfig) else {"local", "api"}
        if config.mode not in modes:
            raise ConfigurationError("Unsupported inference mode")
        if config.url is not None:
            validate_url(config.url, local=config.mode == "local")


class _RedactedConfig:
    def __repr__(self) -> str:
        objects = (self, getattr(self, "stt", None), getattr(self, "llm", None))
        secrets = tuple(getattr(obj, "api_key", None) for obj in objects) + tuple(
            secret for obj in objects for secret in getattr(obj, "_redaction_secrets", ()))
        def safe(value):
            if isinstance(value, _RedactedConfig):
                return render(value)
            if isinstance(value, (str, Path)):
                value = str(value)
                for secret in secrets:
                    if secret:
                        value = value.replace(secret, "[redacted]")
            return repr(value)
        def render(obj):
            return type(obj).__name__ + "(" + ", ".join(
                f"{item.name}={safe(getattr(obj, item.name))}" for item in fields(obj)
                if item.name != "api_key" and not item.name.startswith("_")) + ")"
        return render(self)

    _redaction_secrets = ()


@dataclass(frozen=True, repr=False)
class STTConfig(_RedactedConfig):
    mode: str = "local"
    model: str | None = "whisper-1"
    model_path: Path | None = None
    url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    _redaction_secrets: tuple[str, ...] = field(default=(), repr=False, compare=False)
    whisper_binary: str = "whisper-cli"
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    connect_timeout: float = 10.0
    read_timeout: float = 120.0
    startup_timeout: float = 120.0
    process_timeout: float = 3600.0
    max_retries: int = 2
    cpu_threads: int = 4
    chunk_duration: float = 600.0
    chunk_overlap: float = 1.0
    max_upload_bytes: int = 24_000_000
    vad_model: Path | None = None
    source_language: str = "auto"
    audio_stream: int | None = None
    force_transcribe: bool = False

    def __post_init__(self) -> None:
        _validate_fields(self)
        if self.chunk_overlap >= self.chunk_duration:
            raise ConfigurationError("chunk_overlap must be less than chunk_duration")
        if self.source_language not in {"auto", "ja", "ko", "en", "zh"}:
            raise ConfigurationError("source_language must be auto, ja, ko, en or zh")


@dataclass(frozen=True, repr=False)
class LLMConfig(_RedactedConfig):
    mode: str = "local"
    model: str | None = None
    model_path: Path | None = None
    url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    _redaction_secrets: tuple[str, ...] = field(default=(), repr=False, compare=False)
    server_binary: str = "llama-server"
    managed_server: bool = False
    connect_timeout: float = 10.0
    read_timeout: float = 120.0
    startup_timeout: float = 120.0
    max_retries: int = 2
    cpu_threads: int = 4
    batch_size: int = 20
    agent: str | None = None
    agent_model: str | None = None
    agent_binary: str | None = None
    agent_timeout: float = 300.0

    def __post_init__(self) -> None:
        if self.mode == "agent":
            # Switching a saved local/API profile must not retain its route,
            # weights, managed server, or endpoint-bound credential.
            if self.api_key:
                object.__setattr__(self, "_redaction_secrets", self._redaction_secrets + (self.api_key,))
            for name in ("url", "model", "model_path", "api_key"):
                object.__setattr__(self, name, None)
            object.__setattr__(self, "managed_server", False)
        _validate_fields(self)
        if self.agent not in {None, "codex", "claude", "opencode", "agy"} or (self.mode == "agent" and not self.agent):
            raise ConfigurationError("Agent translation requires --agent codex, claude, opencode or agy")
        if self.agent_timeout > 3600:
            raise ConfigurationError("agent_timeout must be at most 3600 seconds")
        if self.managed_server and self.mode != "local":
            raise ConfigurationError("managed_server requires local LLM mode")


@dataclass(frozen=True, repr=False)
class RunConfig(_RedactedConfig):
    stt: STTConfig = field(default_factory=STTConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    glossary: Path | None = None
    save_source: Path | None = None
    output: Path | None = None
    overwrite: bool = False
    include_media_context: bool = False

    def __post_init__(self) -> None:
        _validate_fields(self)
        if not isinstance(self.stt, STTConfig) or not isinstance(self.llm, LLMConfig):
            raise ConfigurationError("stt and llm must be validated configuration objects")


_CLASSES = {"stt": STTConfig, "llm": LLMConfig, "run": RunConfig}
_NAMES = {section: {item.name for item in fields(cls) if not item.name.startswith("_")} - {"stt", "llm"}
          for section, cls in _CLASSES.items()}


@dataclass(frozen=True)
class _EnvironmentValue:
    value: str = field(repr=False)


def _environment(environ: Mapping[str, str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {section: {} for section in _CLASSES}
    for section, names in _NAMES.items():
        for name in names:
            key = f"MEDIA_TO_SUBS_{section.upper()}_{name.upper()}"
            if key not in environ:
                continue
            # Mark origin, but convert only after higher-priority fields replace
            # these values. A CLI/TOML override must win even over malformed env.
            result[section][name] = _EnvironmentValue(environ[key])
    return result


def _convert_environment(values: dict[str, dict[str, object]]) -> None:
    for section, settings in values.items():
        for name, raw in settings.items():
            if not isinstance(raw, _EnvironmentValue):
                continue
            value = raw.value
            if not isinstance(value, str):
                raise ConfigurationError(f"Invalid environment value for {section}.{name}")
            try:
                if name in _BOOL_FIELDS:
                    if value.lower() not in {"true", "false"}:
                        raise ValueError
                    value = value.lower() == "true"
                elif name in _INT_FIELDS:
                    value = int(value)
                elif name in _FLOAT_FIELDS:
                    value = float(value)
            except ValueError:
                raise ConfigurationError(f"Invalid environment value for {section}.{name}") from None
            settings[name] = value


def _merge(target: dict[str, dict[str, object]], source: Mapping[str, object],
           *, relative_to: Path | None = None) -> None:
    if not isinstance(source, Mapping) or any(section not in _CLASSES for section in source):
        raise ConfigurationError("Configuration sections must be stt, llm or run")
    for section, values in source.items():
        if not isinstance(values, Mapping) or any(name not in _NAMES[section] for name in values):
            raise ConfigurationError(f"Unknown or malformed {section} configuration")
        if "api_key" in values:
            raise ConfigurationError("API keys must come from separate MEDIA_TO_SUBS_STT_API_KEY / MEDIA_TO_SUBS_LLM_API_KEY environment variables")
        for name, value in values.items():
            if relative_to is not None and name in _PATH_FIELDS and isinstance(value, str) and value.strip():
                value = relative_to / value
            target[section][name] = value


def load_config(config_path: Path | None = None, *,
                cli: Mapping[str, Mapping[str, object]] | None = None,
                defaults: Mapping[str, Mapping[str, object]] | None = None,
                environ: Mapping[str, str] | None = None) -> RunConfig:
    """Load only a requested TOML file. CLI mappings omit unspecified flags.

    Env variables use MEDIA_TO_SUBS_{STT|LLM|RUN}_{FIELD}, uppercase.
    Unrelated/unknown environment variables are ignored. No dotenv or settings I/O.
    """
    values = {section: {} for section in _CLASSES}
    if defaults is not None:
        _merge(values, defaults)
    for section, settings in _environment(os.environ if environ is None else environ).items():
        values[section].update(settings)
    if config_path is not None:
        path = Path(config_path).absolute()
        try:
            with path.open("rb") as stream:
                source = tomllib.load(stream)
        except (OSError, ValueError):
            raise ConfigurationError("Cannot read configuration as a valid TOML file") from None
        _merge(values, source, relative_to=path.parent)
    if cli is not None:
        _merge(values, cli)
    _convert_environment(values)
    return RunConfig(stt=STTConfig(**values["stt"]), llm=LLMConfig(**values["llm"]), **values["run"])
