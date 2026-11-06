"""One-time inference choices; persist only a complete, checked transaction."""
import argparse
import getpass
import os
from pathlib import Path
import sys
import warnings

from .config import load_config, validate_url
from .doctor import report
from .errors import CancelledError, ConfigurationError
from .profile import Profile, bind_credentials, load_profile, profile_path, request_url, save_profile


def build_parser():
    from .cli import _Parser
    parser = _Parser(prog="media-to-subs setup", allow_abbrev=False,
                     description="Save private local/API/agent choices. Agent CLIs require their own installed login. Stored API keys are plaintext protected by file permissions.")
    for stage in ("stt", "llm"):
        parser.add_argument("--" + stage, choices=("local", "api", "agent") if stage == "llm" else ("local", "api"))
        parser.add_argument(f"--{stage}-url")
        parser.add_argument(f"--{stage}-model-name")
        parser.add_argument(f"--{stage}-no-key", action="store_true", help="deliberately use a loopback API without authentication")
    for name in ("whisper-model", "vad-model", "llm-model", "glossary"):
        parser.add_argument("--" + name)
    for name in ("whisper", "vad", "llm"):
        parser.add_argument("--download-" + name, action="store_true", help="explicitly download the pinned reference model")
    for name in ("whisper-binary", "ffmpeg-binary", "ffprobe-binary", "server-binary"):
        parser.add_argument("--" + name)
    parser.add_argument("--managed-llm", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--agent", choices=("codex", "claude", "opencode", "agy"))
    parser.add_argument("--agent-model")
    parser.add_argument("--agent-binary")
    parser.add_argument("--agent-timeout", type=float)
    parser.add_argument("--source-language", choices=("auto", "ja", "ko", "en", "zh"))
    parser.add_argument("--yes", action="store_true", help="save without prompts; all required values must be supplied or already saved")
    return parser


def _ask(label, default, *, interactive, choices=None):
    if not interactive:
        value = default
    else:
        suffix = " (Enter keeps current/default)" if default is not None else ""
        try:
            value = input(label + suffix + ": ").strip() or default
        except EOFError:
            raise CancelledError("Setup cancelled") from None
    if value is None or value == "" or (choices is not None and value not in choices):
        raise ConfigurationError("Setup needs a valid " + label + "; use media-to-subs setup --help")
    return value


def _path(value: str) -> str:
    return str(Path(value).expanduser().absolute())


def _model(label, value, old, model_id, *, download, interactive, progress, cancellation):
    if download and value is not None:
        raise ConfigurationError("Choose an existing model or a download, not both")
    choice = "download" if download else value
    if choice is None:
        choice = _ask(label + " path, or download", old, interactive=interactive)
    if choice != "download":
        return _path(choice)
    # Kept lazy so local existing assets need no downloader/manifest import.
    from .model_setup import DEFAULT_MANIFEST, SetupError, fetch_model, load_manifest
    try:
        entry = load_manifest(DEFAULT_MANIFEST)[model_id]
        progress(f"Reference model {model_id}: {entry['bytes']} bytes; license {entry['license']}")
        progress("Download source and license: " + entry["source_url"] + " " + entry["license_url"])
        if interactive and _ask("Download this model? yes/no", "no", interactive=True, choices=("yes", "no")) != "yes":
            raise CancelledError("Setup cancelled")
        data_root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        if not data_root.is_absolute():
            raise ConfigurationError("XDG_DATA_HOME must be absolute")
        cancellation.raise_if_cancelled()
        result = fetch_model(entry, data_root / "media-to-subs/models")
        cancellation.raise_if_cancelled()
        return _path(str(result))
    except SetupError:
        raise ConfigurationError("Model download failed; existing profile is unchanged. Retry setup or select an existing model") from None


def _credential(stage, options, old, *, no_key, interactive, register_secret):
    url = request_url(stage, options["url"])
    prior = old.credentials.get(stage) if old else None
    reusable = prior is not None and prior["mode"] == "api" and prior["url"] == url
    if no_key:
        validate_url(options["url"], local=True)
        return None
    env_name = f"MEDIA_TO_SUBS_{stage.upper()}_API_KEY"
    if env_name in os.environ:
        key = os.environ[env_name]
        register_secret(key)
    elif interactive:
        prompt = f"{stage.upper()} API key (hidden; " + ("Enter keeps saved key" if reusable else "'-' for no key on loopback") + "): "
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ConfigurationError("Hidden key input is unavailable; use a terminal or a namespaced API key environment variable") from None
        except EOFError:
            raise CancelledError("Setup cancelled") from None
        register_secret(key)
        if not key and reusable:
            key = prior["key"]
        if key == "-":
            validate_url(options["url"], local=True)
            return None
    elif reusable:
        key = prior["key"]
    elif (old and stage in old.settings and old.settings[stage].get("mode") == "api"
          and request_url(stage, old.settings[stage]["url"]) == url and prior is None):
        validate_url(options["url"], local=True)
        return None
    else:
        raise ConfigurationError(f"{stage.upper()} needs a fresh key; use hidden interactive setup or {env_name}, or deliberately select --{stage}-no-key for loopback")
    if not key or not key.strip() or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise ConfigurationError("API key must be nonempty and contain no whitespace or control characters")
    return {"mode": "api", "url": url, "key": key}


def run_setup(argv, cancellation, progress, register_secret) -> int:
    args = build_parser().parse_args(argv)
    interactive = not args.yes
    if interactive and not sys.stdin.isatty():
        raise ConfigurationError("Setup needs a terminal; for automation supply complete options with --yes")
    location = profile_path()
    old = load_profile(location)
    if old:
        for key in old.secrets():
            register_secret(key)
    progress("Setup saves API keys as local plaintext protected by directory 0700 and file 0600 permissions")
    settings, credentials = {}, {}
    for stage in ("stt", "llm"):
        cancellation.raise_if_cancelled()
        before = old.settings[stage] if old else {}
        modes = ("local", "api", "agent") if stage == "llm" else ("local", "api")
        mode = getattr(args, stage) or _ask(stage.upper() + " mode (" + "/".join(modes) + ")", before.get("mode", "local"),
                                          interactive=interactive, choices=modes)
        options = dict(before) if mode == before.get("mode") else {}
        options["mode"] = mode
        settings[stage] = options
        if mode == "api":
            options["url"] = getattr(args, stage + "_url") or _ask(stage.upper() + " API URL", before.get("url") if before.get("mode") == "api" else None, interactive=interactive)
            options["model"] = getattr(args, stage + "_model_name") or _ask(stage.upper() + " API model name", before.get("model") if before.get("mode") == "api" else None, interactive=interactive)
            validate_url(options["url"])
            key = _credential(stage, options, old, no_key=getattr(args, stage + "_no_key"),
                              interactive=interactive, register_secret=register_secret)
            if key is not None:
                credentials[stage] = key
        elif mode == "agent":
            options["agent"] = args.agent or _ask("Translation agent (codex/claude/opencode/agy)", options.get("agent"),
                                                  interactive=interactive, choices=("codex", "claude", "opencode", "agy"))
            for name in ("agent_model", "agent_binary", "agent_timeout"):
                value = getattr(args, name)
                if value is not None:
                    options[name] = _path(value) if name == "agent_binary" and ("/" in value or value.startswith("~")) else value
            progress("Agent translation sends subtitle text to the selected CLI; its login, provider and quota apply. No agent tokens are saved")
        elif stage == "stt":
            options.pop("url", None)
            options["model_path"] = _model("Whisper model", args.whisper_model, options.get("model_path"), "whisper-small",
                                          download=args.download_whisper, interactive=interactive, progress=progress, cancellation=cancellation)
            options["vad_model"] = _model("VAD model", args.vad_model, options.get("vad_model"), "silero-v6.2.0",
                                         download=args.download_vad, interactive=interactive, progress=progress, cancellation=cancellation)
        else:
            managed = args.managed_llm
            if managed is None and (args.llm_model or args.download_llm):
                managed = True
            if managed is None:
                default = "managed" if options.get("managed_server", True) else "existing"
                managed = _ask("Local LLM server (managed/existing)", default, interactive=interactive,
                               choices=("managed", "existing")) == "managed"
            options["managed_server"] = managed
            if managed:
                options["model_path"] = _model("LLM model", args.llm_model, options.get("model_path"), "qwen2.5-1.5b-instruct-q4-k-m",
                                              download=args.download_llm, interactive=interactive, progress=progress, cancellation=cancellation)
                options["url"] = args.llm_url or options.get("url") or "http://127.0.0.1:8080/v1"
            else:
                options.pop("model_path", None)
                options["url"] = args.llm_url or _ask("Local LLM URL", options.get("url"), interactive=interactive)
                options["model"] = args.llm_model_name or _ask("Local LLM served model name", options.get("model"), interactive=interactive)
            validate_url(options["url"], local=True)
    settings["stt"]["source_language"] = args.source_language or _ask("Source language (auto/ja/ko/en/zh)",
        old.settings["stt"].get("source_language", "auto") if old else "auto", interactive=interactive,
        choices=("auto", "ja", "ko", "en", "zh"))
    for name in ("whisper_binary", "ffmpeg_binary", "ffprobe_binary", "server_binary"):
        value = getattr(args, name)
        if value:
            settings["llm" if name == "server_binary" else "stt"][name] = _path(value) if "/" in value or value.startswith("~") else value
    run_options = dict(old.settings.get("run", {})) if old else {}
    if args.glossary:
        run_options["glossary"] = _path(args.glossary)
    if run_options:
        settings["run"] = run_options
    profile = Profile(settings, credentials)
    config = bind_credentials(load_config(defaults=settings, environ={}), profile, {})
    if not report(config, progress):
        raise ConfigurationError("Setup is incomplete; resolve missing items and rerun setup. Previous profile is unchanged")
    cancellation.raise_if_cancelled()
    if interactive and _ask("Save these choices? yes/no", "yes", interactive=True, choices=("yes", "no")) != "yes":
        raise CancelledError("Setup cancelled")
    save_profile(profile, location, cancellation=cancellation)
    progress("Setup saved. Run media-to-subs MEDIA, or media-to-subs doctor to check dependencies")
    return 0
