"""Selected dependency checks only: no engine execution, HTTP, or inference."""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from .config import RunConfig


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str


def _binary(label: str, binary: str, formula: str) -> Check:
    found = shutil.which(binary) is not None
    return Check(label, found, "available" if found else
                 f"missing; with Homebrew on macOS/Linux: brew install {formula} (https://formulae.brew.sh/formula/{formula})")


def _asset(label: str, path: Path | None) -> Check:
    try:
        available = path is not None and path.is_file() and path.stat().st_size > 0 and os.access(path, os.R_OK)
    except OSError:
        available = False
    return Check(label, available, "readable file" if available else "missing, empty or unreadable; rerun media-to-subs setup")


def check_config(config: RunConfig) -> list[Check]:
    """Messages use fixed labels so paths/model names cannot disclose credentials."""
    checks = [_binary("FFmpeg", config.stt.ffmpeg_binary, "ffmpeg"),
              _binary("FFprobe", config.stt.ffprobe_binary, "ffmpeg")]
    if config.stt.mode == "local":
        checks.extend((_binary("whisper-cli", config.stt.whisper_binary, "whisper.cpp"),
                       _asset("Whisper model", config.stt.model_path)))
        if config.stt.vad_model is not None:
            checks.append(_asset("VAD model", config.stt.vad_model))
    if config.llm.mode == "local" and config.llm.managed_server:
        checks.extend((_binary("llama-server", config.llm.server_binary, "llama.cpp"),
                       _asset("LLM model", config.llm.model_path)))
        route = urlsplit((config.llm.url or "http://127.0.0.1:8080/v1").rstrip("/"))
        supported = route.scheme == "http" and route.path in ("/v1", "/v1/chat/completions")
        checks.append(Check("Managed LLM route", supported,
                            "configured" if supported else "requires an HTTP loopback /v1 endpoint"))
    for stage in ("stt", "llm"):
        options = getattr(config, stage)
        if options.mode == "agent":
            found = shutil.which(options.agent_binary or options.agent) is not None
            checks.append(Check("Translation agent CLI", found,
                                "available; installed login required, capabilities checked before translation"
                                if found else "missing; install and log into the selected CLI separately, or set --agent-binary"))
            checks.append(Check("Agent login and quota", True, "not tested; doctor does not perform inference"))
        elif options.mode == "api" or (stage == "llm" and not options.managed_server):
            valid = bool(options.url and options.model)
            checks.append(Check(stage.upper() + " endpoint and model", valid,
                                "configured (not contacted)" if valid else "required; run media-to-subs setup"))
        if options.mode == "api":
            checks.append(Check(stage.upper() + " credential", True,
                                "configured (hidden)" if options.api_key else "not configured"))
    if config.glossary:
        checks.append(_asset("Glossary", config.glossary))
    return checks


def report(config: RunConfig, progress) -> bool:
    checks = check_config(config)
    for check in checks:
        progress(f"{'OK' if check.ok else 'MISSING'} {check.label}: {check.detail}")
    progress("Doctor checks local files and executables only; model compatibility and endpoints were not tested")
    return all(check.ok for check in checks)
