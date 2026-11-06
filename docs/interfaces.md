# Foundation contracts

This file records the implemented module contracts for the standalone CLI.
Python 3.11+, package `media_to_subs`, standard-library unittest. Times are seconds
on the original media timeline. Inputs and authored sidecars are never scratch.

## Timed text

```python
@dataclass(frozen=True)
class Segment:
    id: str
    start: float
    end: float
    text: str

@dataclass(frozen=True)
class TimedTranscript:
    language: str
    segments: tuple[Segment, ...]
    provenance: Mapping[str, object]
    allow_overlaps: bool = False  # keyword-only; authored subtitles only

    def validate(self, *, allow_overlaps: bool = False,
                 duration: float | None = None) -> None: ...

def parse_srt(text: str, *, language: str = "und",
              provenance: Mapping[str, object] | None = None,
              allow_overlaps: bool = False) -> TimedTranscript: ...
def serialize_srt(transcript: TimedTranscript, *, allow_overlaps: bool = False) -> str: ...
```

Segment IDs are stable, nonempty, whitespace-free strings. Times must be finite,
nonnegative and end strictly after start. Cues stay in supplied order; IDs must
be unique and starts cannot decrease. Overlap is rejected by default, including
when calling `validate()` or `serialize_srt()` on an overlap-enabled transcript.
Adapters producing inferred ASR must use strict validation. Empty transcripts
represent silence; empty SRT input/output is rejected. Text is never stripped or
translated by foundation. Collections and JSON-like provenance are copied into
immutable snapshots, including nested mappings/sequences. No secrets in provenance.

SRT supports positive integer cue numbers, comma milliseconds, multiline text,
UTF-8 BOM and CRLF input. It rejects timing extensions, missing/duplicate numbers,
invalid timestamps and blank cue content. Parsed cue numbers become string IDs.
Serialization preserves all IDs if all are positive integer strings; otherwise
it assigns consecutive SRT numbers without mutating IDs in the transcript. Numeric
IDs must also be numerically unique. Time is rounded to milliseconds (half up);
collapsed cues are rejected. Line endings normalize to LF; no formatting markup
is interpreted. Internal blank lines from converted ASS cues are preserved;
cue boundaries require an index and timing header. Malformed headers and timing
lines embedded ambiguously in dialogue are rejected. Leading/trailing blank
lines inside cue text are not serializable.

## Adapter interfaces

```python
from pathlib import Path
from collections.abc import Callable, Mapping
from media_to_subs.config import STTConfig, LLMConfig
from media_to_subs.runtime import Cancellation
from media_to_subs.types import TimedTranscript

Progress = Callable[[str], None]  # plain stage/status message, no secrets

def transcribe(media_path: Path, config: STTConfig, scratch: Path,
               cancellation: Cancellation, progress: Progress) -> TimedTranscript: ...
def discover(media_path: Path, config: STTConfig, scratch: Path,
             cancellation: Cancellation, progress: Progress) -> TimedTranscript | None: ...
def translate(transcript: TimedTranscript, config: LLMConfig,
              glossary: Mapping[str, str] | None, cancellation: Cancellation,
              progress: Progress) -> TimedTranscript: ...
```

`discover` returns None only when no suitable subtitles exist, and respects
`config.force_transcribe`. It may opt into authored overlaps explicitly.
`transcribe` returns strict timing, with chunk offsets already applied and overlap
deduplicated. `translate` preserves each ID, start, end, order and overlap policy,
changing only text/language/provenance. Source language, selected stream, engine,
model, and relevant options belong in provenance (JSON-compatible, no credentials).
Provider failure raises an error; never fall back from local to API implicitly.
Only check engine binary/model presence when that engine is actually needed.

## Configuration

Frozen dataclasses in `config.py`, with validated types/ranges:

| Class | Fields and defaults |
| --- | --- |
| STTConfig | `mode="local"`, `model="whisper-1"`, `model_path=None`, `url=None`, `api_key=None` (repr excluded), `whisper_binary="whisper-cli"`, `ffmpeg_binary="ffmpeg"`, `ffprobe_binary="ffprobe"`, `connect_timeout=10.0`, `read_timeout=120.0`, `startup_timeout=120.0`, `process_timeout=3600.0`, `max_retries=2`, `cpu_threads=4`, `chunk_duration=600.0`, `chunk_overlap=1.0`, `max_upload_bytes=24000000`, `vad_model=None`, `source_language="auto"`, `audio_stream=None`, `force_transcribe=False` |
| LLMConfig | `mode="local"`, `model=None`, `model_path=None`, `url=None`, `api_key=None` (repr excluded), `server_binary="llama-server"`, `managed_server=False`, `connect_timeout=10.0`, `read_timeout=120.0`, `startup_timeout=120.0`, `max_retries=2`, `cpu_threads=4`, `batch_size=20` |
| RunConfig | `stt=STTConfig()`, `llm=LLMConfig()`, `glossary=None`, `save_source=None`, `output=None`, `overwrite=False`, `include_media_context=False` |

Mode is `local` or `api`. Model is an API/served model name; model_path and vad_model
are optional Paths. URL is an optional full base URL (adapters append their endpoint
path exactly once); None means unset, not a hidden provider fallback. Local LLM
adapters may default an unset URL to `http://127.0.0.1:8080/v1`. API adapters require
explicit URLs before use. `managed_server` launches only in local mode. Language
is `auto`, `ja`, `ko`, `en` or `zh`; stream is a nonnegative absolute media stream
index, or None. Other path fields are Path; binary fields are strings. Timeouts
and chunk fields are seconds. max_retries counts retries after the first attempt.
Engine existence and mode-specific required models/URLs are deferred to adapters.
`include_media_context` is an explicit boolean opt-in for title/folder context;
the default sends no media path context. The final CLI owns its flag and behavior.

```python
def load_config(config_path: Path | None = None, *,
                cli: Mapping[str, Mapping[str, object]] | None = None,
                environ: Mapping[str, str] | None = None,
                defaults: Mapping[str, Mapping[str, object]] | None = None) -> RunConfig: ...
def validate_url(url: str, *, local: bool = False) -> None: ...
```

TOML accepts only `[stt]`, `[llm]`, `[run]` tables with the corresponding fields
above. CLI uses the same nested mapping shape and must omit unspecified options
(argparse `SUPPRESS` is appropriate). Each provided field overrides TOML, then
environment, supplied saved defaults, then the dataclass default; explicit False overrides True. Unknown
sections/fields and malformed types/ranges fail with ConfigurationError.
Environment conversion happens only after overrides are merged: an invalid
lower-priority env value is ignored if that same field is supplied by TOML/CLI;
invalid surviving values still fail. CLI/TOML values are never implicitly coerced.
TOML path fields resolve relative to the explicit TOML file's directory. CLI/env
paths are relative to the current directory; no implicit tilde/variable expansion.
Binary strings are executable names or paths, never shell expressions.

Each field has an optional environment default named
`MEDIA_TO_SUBS_{STT|LLM|RUN}_{FIELD}` in uppercase, e.g.
`MEDIA_TO_SUBS_STT_MAX_UPLOAD_BYTES=24000000` or
`MEDIA_TO_SUBS_LLM_BATCH_SIZE=20`. Integers and numbers parse as such; booleans
accept only `true`/`false` (case insensitive). Unrelated/unknown environment
variables are ignored. Defaults are listed in the table; no generic provider
environment variables are read. Passing `environ={}` makes loading independent
of the process environment.

The only key inputs to the loader are `MEDIA_TO_SUBS_STT_API_KEY` and
`MEDIA_TO_SUBS_LLM_API_KEY`; CLI/TOML `api_key` is rejected. Programmatic adapters
may receive an explicitly constructed config with a key. Keys are excluded from
dataclass repr, but dataclasses.asdict is **not** a safe logging format. Never log
config internals, provider bodies, subprocess arguments containing secrets, or
authorization headers. No implicit `.env` file or unrelated settings are read.
The loader does not check binary/model existence or require unused engines.

The loader is pure with respect to user profiles: `defaults` must be supplied
explicitly. The CLI loads its intentional private profile unless `--config` or
`--no-profile` bypasses it. `profile.bind_credentials` adds a saved stage key only
when that stage remains in API mode and its normalized request endpoint exactly
matches the saved endpoint. Explicit namespaced environment keys take precedence.
Local stages discard API keys. Profile settings never persist output, overwrite
or source-export destinations. The CLI protects the profile and explicit TOML
from input/output aliases. Help and version do not load a profile.

`setup` checks selected dependencies and atomically saves settings and credentials
in one owner-only file; failure or cancellation preserves prior settings. `doctor`
checks selected local files and executables without starting engines or making
network requests. Its success does not validate model formats or API credentials.

URLs require HTTP(S), no userinfo, query, fragment, control characters, backslash,
or encoded host. Local URLs accept only `localhost` or literal loopback IPs;
validation does not resolve DNS. Adapters must disable redirects or enforce the
same origin and local-address rules at each request; syntax validation alone does
not police an HTTP client's redirects/proxies.

## Errors

`MediaToSubsError` is the common base. `ConfigurationError` also subclasses
ValueError. `ProcessingError` covers `ProviderError`, `ValidationError`
(also ValueError), `ProcessTimeoutError`, and `FileSafetyError`.
`CancelledError` is separate from processing failure. Messages omit raw config
values, credentials and provider payloads. CLI exit-code/signal policy belongs
to the final CLI worker.

## File publication

```python
def validate_output_path(output: Path, *, input_path: Path,
                         sidecars: Iterable[Path] = (), overwrite: bool = False) -> Path: ...
def atomic_publish(output: Path, data: str | bytes, *, input_path: Path,
                   validator: Callable[[bytes], None], sidecars: Iterable[Path] = (),
                   overwrite: bool = False, cancellation: Cancellation | None = None) -> Path: ...
@contextmanager
def scratch_directory(parent: Path | None = None) -> Iterator[Path]: ...
```

`validate_output_path` rejects input/sidecar aliases (including hard links), final
symlinks, nonregular destinations, missing output directories, and existing files
unless overwrite is explicit. It returns an absolute path with canonical parent.
`atomic_publish` repeats safety checks, validates the exact UTF-8 bytes to publish
(strings are encoded), stages/fsyncs a sibling temporary file, then commits atomically.
The required validator raises on invalid data, e.g.
`lambda data: parse_srt(data.decode("utf-8"), language="zh")`. The publication
function returns the output Path. Pass all discovered/user-owned sidecars and any
other output that must be preserved in `sidecars`; prepare source and final output
destinations before either publication. Atomicity is per file, not a multi-output
transaction. No-clobber is enforced at the commit syscall, including races.
Failed validation/write/replace retains the old output. Cancellation is checked
before staging and before commit. A cancellation after commit cannot undo publication.
Only private sibling temporary files and the exact per-run scratch directory are
cleaned. Scratch context cleanup does not traverse symlinks to user directories.
Directories must already exist. Publication uses POSIX directory descriptors and
is tested on macOS; native Windows is not claimed. Existing ancestor symlinks are
canonicalized before opening; operations then use the held directory descriptor,
and parent changes detected before commit fail. Final output symlinks are rejected;
if one is raced in after the last overwrite check, atomic replace replaces only
that directory entry and never follows it to its target. No-clobber still fails.
The staged file is fsynced before commit; directory-entry persistence across sudden
power loss is not promised. Staging cleanup errors after a successful no-clobber
commit explicitly report that output was published. Scratch identity replacement
fails closed, leaving the replacement and moved original for caller inspection.

## Runtime

```python
class Cancellation:
    def cancel(self) -> None: ...
    @property
    def cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...
    def wait(self, timeout: float | None = None) -> bool: ...

@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str  # repr excluded
    stderr: str  # repr excluded

def run_process(args: Sequence[str | os.PathLike[str]], *, timeout: float,
                cancellation: Cancellation, cwd: Path | None = None,
                env: Mapping[str, str] | None = None, check: bool = True,
                input_text: str | bytes | None = None) -> ProcessResult: ...
```

`input_text` passes JSON (including explicit credentials) over stdin instead of argv.
Strings encode as UTF-8; bytes
pass unchanged; writes participate in the same deadline/cancellation loop. Never
include secrets in args, progress or errors. Captured output is UTF-8 with invalid
bytes replaced and is caller-private; check=False returns nonzero status for callers
to classify. Default failure messages include status only, no command/output.
No shell expansion or global signal handlers. POSIX commands use a new owned process
group and terminate descendants on timeout/cancellation; unrelated servers/processes
are never discovered or killed. Timeout is a monotonic execution budget plus bounded
cleanup grace. Inference binaries must not daemonize/escape their group.

```python
@contextmanager
def owned_process(args: Sequence[str | os.PathLike[str]], *, cancellation: Cancellation,
                  cwd: Path | None = None, env: Mapping[str, str] | None = None,
                  stdout: IO[bytes] | int = subprocess.DEVNULL,
                  stderr: IO[bytes] | int = subprocess.DEVNULL) -> Iterator[subprocess.Popen[bytes]]: ...
```

Managed servers use `with owned_process(...) as process:`. The yielded standard
Popen supports poll/wait; context exit terminates only its owned group, including
on exceptions. Use caller-owned scratch log file handles for stdout/stderr or the
default DEVNULL; PIPE is rejected to prevent unconsumed output deadlocks.
Readiness, startup_timeout and cancellation polling belong to the LLM adapter;
there is no background cancellation monitor in this context manager. An adapter
must call cancellation.raise_if_cancelled() during readiness/requests and leave
the context on failure. Existing endpoints remain entirely caller-owned and must
never be passed to owned-process cleanup. Private underscore-prefixed helpers are
not integration APIs. POSIX group behavior is tested on macOS; Windows process-tree
support is not claimed. run_process holds captured output in memory, so adapters
should choose quiet binary flags and bounded provider response sizes.


## Installed translation CLI adapter

`LLMConfig.mode` also accepts `agent`, with `agent` selected from `codex`, `claude`,
`opencode` and `agy`. `agent_model` and `agent_binary` are optional; `agent_timeout` is a
positive finite deadline up to 3600 seconds, defaulting to 300. STT stays local/API.
`AgentClient.chat(messages)` returns validated envelope text to the existing subtitle
parser. The translator retains batching, one content-correction attempt, immutable
source timings and atomic publication. Provider/process/envelope errors do not retry.

Provenance uses the selected CLI as `engine`, `mode=agent`, and either the explicit
model with `model_source=configured` or null with `model_source=cli-default`.
`run_process(max_output_bytes=...)` optionally bounds combined stdout/stderr capture
without changing existing unbounded call sites. The [agent contract](agent-translation-contract.md)
records exact flags, environment, event parsing and capability limits.

Antigravity 1.4.12 reports a shared tool registry in its init envelope. The adapter
validates that metadata separately from permissions, explicitly attaches its
private deny-all hook file with customization inheritance disabled, and rejects
every actual tool event. See the [Antigravity contract](antigravity-translation-contract.md).
