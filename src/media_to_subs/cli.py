"""One invocation owns its handlers, cancellation token and processing resources.

SIGINT and SIGTERM both request cancellation and exit 130 after owned cleanup.
Handlers are installed only during main-thread invocation and restored on
every return path. A signal after an atomic commit cannot undo that publication.
"""

import argparse
from collections.abc import Sequence
from contextlib import contextmanager
import os
from pathlib import Path
import re
import signal
import sys
import threading

from . import __version__
from .config import load_config
from .errors import CancelledError, ConfigurationError, FileSafetyError, MediaToSubsError
from .files import validate_output_path
from .pipeline import run
from .profile import bind_credentials, load_profile, profile_path
from .runtime import Cancellation


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual message echoes rejected values and unknown arguments,
        # which may contain credentials accidentally pasted onto the command line.
        raise ConfigurationError("Invalid command line; use --help for supported options")


def build_parser(*, doctor: bool = False) -> argparse.ArgumentParser:
    parser = _Parser(
        prog="media-to-subs doctor" if doctor else "media-to-subs",
        description="Translate one local media file to Chinese SRT using existing subtitles or local/API/agent inference.",
        argument_default=argparse.SUPPRESS,
        allow_abbrev=False,
        epilog=("Commands: setup saves inference choices; doctor checks required local dependencies. "
                "Use ./setup or ./doctor for media files with those names. "
                "Settings: flags > explicit TOML > MEDIA_TO_SUBS_* environment > saved profile > defaults. "
                "Keys: hidden setup prompts or MEDIA_TO_SUBS_STT_API_KEY / MEDIA_TO_SUBS_LLM_API_KEY. "
                "Exit codes: 0 success (including silence), 2 usage/config, 1 processing, "
                "130 SIGINT/SIGTERM cancellation. Existing authored sidecars are never overwritten; "
                "an exact selected Chinese SRT destination is reused unchanged. "
                "Publication is atomic per file; a completed file survives later export failure."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    if not doctor:
        parser.add_argument("media", type=Path, help="one local video or audio file")
    parser.add_argument("--config", type=Path, help="explicit TOML configuration file")
    parser.add_argument("--no-profile", action="store_true", help="bypass saved settings and credentials (--config also bypasses them)")
    parser.add_argument("--stt", dest="stt.mode", choices=("local", "api"), help="transcription backend")
    parser.add_argument("--llm", dest="llm.mode", choices=("local", "api", "agent"), help="translation backend")
    parser.add_argument("--agent", dest="llm.agent", choices=("codex", "claude", "opencode", "agy"), help="installed translation CLI with its own login")
    parser.add_argument("--agent-model", dest="llm.agent_model", help="optional CLI model override; otherwise its default")
    parser.add_argument("--agent-binary", dest="llm.agent_binary", help="executable name or path (no command template)")
    parser.add_argument("--agent-timeout", dest="llm.agent_timeout", type=float, help="per-agent request deadline in seconds (default 300, maximum 3600)")
    parser.add_argument("--whisper-model", dest="stt.model_path", type=Path, metavar="PATH", help="local whisper.cpp model file")
    parser.add_argument("--vad-model", dest="stt.vad_model", type=Path, metavar="PATH", help="local VAD model file")
    parser.add_argument("--llm-model", dest="llm.model_path", type=Path, metavar="PATH", help="managed llama.cpp model file")
    parser.add_argument("--stt-model-name", dest="stt.model", metavar="NAME", help="API transcription model name")
    parser.add_argument("--llm-model-name", dest="llm.model", metavar="NAME", help="served/API translation model name")
    parser.add_argument("--stt-url", dest="stt.url", metavar="URL", help="explicit transcription API base URL")
    parser.add_argument("--llm-url", dest="llm.url", metavar="URL", help="existing endpoint or managed loopback base URL")
    parser.add_argument("--managed-llm", dest="llm.managed_server", action=argparse.BooleanOptionalAction,
                        help="start an owned local llama-server; otherwise use an existing endpoint")
    parser.add_argument("--source-language", dest="stt.source_language", choices=("auto", "ja", "ko"))
    parser.add_argument("--audio-stream", dest="stt.audio_stream", type=int, metavar="INDEX", help="absolute media stream index")
    parser.add_argument("--force-transcribe", dest="stt.force_transcribe", action=argparse.BooleanOptionalAction,
                        help="bypass existing subtitle discovery")
    parser.add_argument("--stt-threads", dest="stt.cpu_threads", type=int, metavar="N")
    parser.add_argument("--llm-threads", dest="llm.cpu_threads", type=int, metavar="N")
    parser.add_argument("--output", dest="run.output", type=Path, metavar="PATH", help="target SRT (default: MEDIA.zh.srt)")
    parser.add_argument("--glossary", dest="run.glossary", type=Path, metavar="PATH", help="explicit JSON source-to-target glossary")
    parser.add_argument("--save-source", dest="run.save_source", type=Path, metavar="PATH", help="also export timed source SRT")
    parser.add_argument("--overwrite", dest="run.overwrite", action=argparse.BooleanOptionalAction,
                        help="replace output files; input assets and authored sidecars stay protected")
    parser.add_argument("--include-media-context", dest="run.include_media_context", action=argparse.BooleanOptionalAction,
                        help="send parent folder name and media stem as translation context (off by default)")
    return parser


@contextmanager
def _signals(cancellation, *, interrupt: bool = False):
    previous = {}
    def cancel(*_):
        cancellation.cancel()
        if interrupt:
            raise CancelledError("Cancelled")
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, cancel)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _safe_text(message: str, secrets=()) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    for key in ("MEDIA_TO_SUBS_STT_API_KEY", "MEDIA_TO_SUBS_LLM_API_KEY"):
        secret = os.environ.get(key)
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "[endpoint]", message)
    return " ".join(message.split())[:500]


def _progress(message: str, secrets=()) -> None:
    print("media-to-subs: " + _safe_text(message, secrets), file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    secrets = []
    def progress(message):
        _progress(message, secrets)
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args and args[0] in {"setup", "doctor"} else None
    try:
        cancellation = Cancellation()
        with _signals(cancellation, interrupt=command == "setup"):
            try:
                if command == "setup":
                    from .setup import run_setup
                    result = run_setup(args[1:], cancellation, progress, secrets.append)
                    cancellation.raise_if_cancelled()
                    return result
                values = vars(build_parser(doctor=command == "doctor").parse_args(args[1:] if command else args))
            except SystemExit as exc:  # --help/--version require no files or engines.
                cancellation.raise_if_cancelled()
                return int(exc.code)
            cancellation.raise_if_cancelled()
            media = values.pop("media", None)
            config_path = values.pop("config", None)
            no_profile = values.pop("no_profile", False)
            overrides = {}
            for destination, value in values.items():
                section, field = destination.split(".", 1)
                overrides.setdefault(section, {})[field] = value
            location = profile_path()
            saved = None if config_path is not None or no_profile else load_profile(location)
            if saved:
                secrets.extend(saved.secrets())
            config = load_config(config_path, cli=overrides, defaults=saved.settings if saved else None)
            cancellation.raise_if_cancelled()
            if saved:
                config = bind_credentials(config, saved, os.environ)
            if (saved is None and config_path is None and not no_profile and not overrides
                    and not any(key.startswith("MEDIA_TO_SUBS_") for key in os.environ)):
                raise ConfigurationError("No saved setup; run media-to-subs setup, or use explicit flags / --config / --no-profile")
            if command == "doctor":
                from .doctor import report
                return 0 if report(config, progress) else 2
            # Protect the private profile even when its settings are bypassed.
            if media.resolve() == location.resolve() or (media.exists() and location.exists() and media.samefile(location)):
                raise FileSafetyError("Input aliases the private profile")
            for path in (config.output or media.with_suffix(".zh.srt"), config.save_source):
                if path is not None:
                    validate_output_path(path, input_path=location, overwrite=True)
            if config_path is not None:
                for path in (config.output or media.with_suffix(".zh.srt"), config.save_source):
                    if path is not None:
                        # The pipeline handles destination existence/reuse. This
                        # check protects the explicit config against all aliases.
                        validate_output_path(path, input_path=config_path, overwrite=True)
            run(media, config, cancellation, progress)
            cancellation.raise_if_cancelled()
        return 0
    except ConfigurationError as exc:
        progress(str(exc))
        return 2
    except (CancelledError, KeyboardInterrupt):
        progress("Cancelled")
        return 130
    except MediaToSubsError as exc:
        progress(str(exc))
        return 1
    except (OSError, UnicodeError):
        progress("Cannot read or write required files; check paths, permissions and disk space")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
