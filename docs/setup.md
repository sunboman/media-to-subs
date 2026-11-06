# Setup

The standalone CLI is implemented and locally verified. See
[verification evidence](verification.md) for platforms, actual-model results,
clean installation, offline Docker and hosted-provider gaps.

## Install the command

From this repository on macOS or Linux:

```sh
./install.sh
media-to-subs setup
```

Python 3.11+ with `venv` and `pip` is required for installation. The installer
keeps the package and dependencies in a private environment for your user and
adds the `media-to-subs` command to your user command directory. There is no
runtime need for `uv`, activation or the current working directory to be this
repository. If the user command directory is missing from PATH, follow the single
PATH instruction printed by the installer. It does not edit your shell settings.

An existing unrelated command is preserved. Re-running the installer updates an
owned installation only after the new environment passes checks; a failed install
keeps the previous command available. It never downloads models or changes your
saved profile. See [installer details](installation-contract.md) for exact paths,
options and uninstall behavior.

## Configure once

```sh
media-to-subs setup
```

Choose transcription and translation independently: local engines or APIs, with
an additional installed-agent option for translation.
For local inference, select existing model paths or explicitly download the pinned
reference weights. For an API, provide its URL, model and key at the hidden prompt.
A local LLM may be managed by the command or supplied as an existing loopback server.
Setup saves these choices and checks the selected dependencies; it does not send
media to a provider to test your credentials.

Normal use is then:

```sh
media-to-subs video.mkv
```

Output defaults to `video.zh.srt`. Rerun setup to change your choices. To diagnose
missing files or tools without inference:

```sh
media-to-subs doctor
```

The saved profile is `~/.config/media-to-subs/profile.json`, or
`$XDG_CONFIG_HOME/media-to-subs/profile.json`. It uses an owner-only file inside a
private directory. Saved keys are plaintext protected by those permissions, not
encrypted; protect backups too. Downloads default to
`${XDG_DATA_HOME:-$HOME/.local/share}/media-to-subs/models`. Existing model files
can stay wherever you keep them; setup saves their absolute paths.

## System tools

All processing modes need FFmpeg/FFprobe for media inspection and decoding. Local
transcription additionally needs `whisper-cli`; managed local translation needs
`llama-server`. API stages need neither local engine nor local model. Setup and
doctor report only the tools required by your choices.

On macOS with Homebrew, these commands install the relevant tools once:

```sh
brew install ffmpeg                  # All modes
brew install whisper.cpp            # Local transcription
brew install llama.cpp              # Managed local translation
```

Formula references: [FFmpeg](https://formulae.brew.sh/formula/ffmpeg),
[whisper.cpp](https://formulae.brew.sh/formula/whisper.cpp),
[llama.cpp](https://formulae.brew.sh/formula/llama.cpp). On Linux, install Python's
venv/pip support and FFmpeg through your distribution, then use the upstream
[whisper.cpp build instructions](https://github.com/ggml-org/whisper.cpp/tree/d1be6fde11ac6e0407606b4e42fe72d34add8037)
and [llama-server documentation](https://github.com/ggml-org/llama.cpp/blob/620d2225cc436ab0b001e3200e590e3b286ef9ab/tools/server/README.md)
when choosing local inference. The Docker recipe below includes an optional build
of those pinned CPU engines. No administrator commands run silently during setup.

For development, `uv sync --frozen`, `uv run --frozen media-to-subs ...`, and
`uv build` remain supported. The package has no web server or database dependency.

## Explicit model setup

Model weights are never packaged or fetched by pip/uv installation. Inspect the
manifest's source, license, revision, exact size and checksum before selecting IDs:

```sh
python3 scripts/fetch_models.py --list
python3 scripts/fetch_models.py --model-dir models whisper-small silero-v6.2.0
python3 scripts/fetch_models.py --model-dir models qwen2.5-1.5b-instruct-q4-k-m
```

| Manifest ID | Download bytes | Upstream license label |
| --- | ---: | --- |
| `whisper-small` | 487,601,967 | MIT |
| `silero-v6.2.0` | 885,098 | MIT |
| `qwen2.5-1.5b-instruct-q4-k-m` | 1,117,320,736 | Apache-2.0 |

Together these occupy 1,605,807,801 bytes (about 1.50 GiB), excluding scratch,
binaries and runtime memory. A download is streamed into a sibling `.part` file;
publication uses a hard link and does not need a second full copy. Keep enough
free space for the remaining bytes and any old model you move aside. These are
small reference artifacts for compatibility checks, not quality recommendations.

`--model-dir /absolute/path` selects storage; set the matching config model paths
explicitly. `--manifest another-manifest.json` accepts a reviewed manifest with
schema 1 and the same pinned metadata. It does not discover arbitrary model names
or trust a moving `main` revision. This script currently supports macOS/Linux and
requires a local filesystem with hard links and advisory file locking.

A valid final file is fully rehashed on every cache hit without network access.
Invalid existing files are preserved and fail clearly. The script has no overwrite
flag. Move an invalid final file aside yourself after inspection. Never use an
input/media file as a model/cache path.

An interrupted transfer leaves `<filename>.part`; rerun the same command to
resume. `206` responses must match the requested offset and manifest total. If the
server ignores Range with `200`, the partial download restarts. A completed partial
is checked locally before any request. A wrong checksum, oversized partial or
corrupt prefix fails without publishing; move/remove that partial to restart.
Do not manually change files while setup is active. Persistent `.lock` files are
normal; OS locks are released on exit, including a crash.

Network operations default to a 30-second socket timeout and a two-hour transfer
deadline. Override with `--timeout` (up to 300 seconds) and `--max-seconds` (up to
86,400 seconds). The deadline is checked between streamed reads, so the final
blocked operation can take one more socket timeout. HTTP/network/disk errors stop
without automatic retries; rerun explicitly. The script does not invoke a shell.

## Configuration and credentials

Precedence is CLI flags → explicitly supplied TOML → namespaced environment
variables → saved user defaults → built-in defaults. An explicit `--config`
bypasses the saved profile and its keys completely; `--no-profile` disables saved
defaults. No `.env` or current-directory config is implicitly read. Unknown TOML
fields are errors. Exact contracts live in [interfaces.md](interfaces.md).

TOML sections are `[stt]`, `[llm]` and `[run]`. TOML paths resolve relative to the
config file; CLI/environment paths resolve relative to the invocation directory.
The checked-in profiles use `../models` because they live in `examples/`.

| Setting | Purpose |
| --- | --- |
| `[stt].mode`, `[llm].mode` | Independent `local` / `api` choices |
| `model` | API or served-model identifier |
| `model_path` | Local Whisper `.bin` or local LLM `.gguf` path |
| `url` | Explicit API base URL, e.g. `https://api.openai.com/v1`; adapters append the route |
| `[stt].vad_model` | Optional local Silero VAD model path |
| `[stt].source_language` | Examples use `ja`; use `ko` for Korean speech or `auto` for detection |
| `[stt].audio_stream` | Absolute FFprobe stream index, not an audio-only ordinal |
| `[llm].managed_server` | `true` starts an owned local server; `false` uses an existing endpoint |
| `cpu_threads`, `read_timeout`, `startup_timeout` | Per-stage resource/time limits |
| `[llm].batch_size` | Cues sent in one translation batch |
| `[run].output`, `save_source`, `glossary`, `overwrite` | Explicit output/export and translation options |
| `[run].include_media_context` / `--include-media-context` | Default `false`; opt in to sending basename/title and immediate parent folder name to translation only |

Environment variables follow `MEDIA_TO_SUBS_{STT|LLM|RUN}_{FIELD}` in uppercase.
For explicit TOML or per-run configuration, supply keys through:

- `MEDIA_TO_SUBS_STT_API_KEY` for transcription.
- `MEDIA_TO_SUBS_LLM_API_KEY` for translation.

Ordinary saved-profile use needs no exports after setup. For an explicit-config
run, set these through a secret manager or a non-echoing shell prompt; never put key
values in arguments or TOML. For example, in Bash (only needed for API stages):

```bash
read -r -s -p 'STT API key: ' MEDIA_TO_SUBS_STT_API_KEY; echo
export MEDIA_TO_SUBS_STT_API_KEY
read -r -s -p 'LLM API key: ' MEDIA_TO_SUBS_LLM_API_KEY; echo
export MEDIA_TO_SUBS_LLM_API_KEY
```

The four profiles in [the README](../README.md#advanced-configuration) use matching actual
config fields. No provider is a hidden fallback. API profiles reference the
Whisper multipart timestamp protocol and a chat-completions model; they have not
been authenticated against paid providers in hosted-provider tests.

**Hosted reference lifetime:** as checked on 2026-10-25, OpenAI documents
`whisper-1` timestamp compatibility only until February 26, 2027, and GPT-4.1 mini
shutdown on January 14, 2027. Recheck availability before using these examples.
A replacement STT model must return usable timed segments; accepting audio or
returning plain text is insufficient. See the official
[transcription guide](https://developers.openai.com/api/docs/guides/speech-to-text),
[GPT-4.1 mini protocol information](https://developers.openai.com/api/docs/models/gpt-4.1-mini),
and [deprecations](https://developers.openai.com/api/docs/deprecations). No replacement
or timestamp invention happens automatically.

For an existing local llama-server, start it separately using a known model alias:

```sh
llama-server --model models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
  --alias qwen2.5-1.5b-instruct --host 127.0.0.1 --port 8080
```

Then use `examples/local-existing.toml`. Local endpoints must be loopback. The CLI
must not terminate an existing server, take over an occupied port, or silently
switch to API mode. With `managed_server = true`, the CLI starts its own server
and cleans it up after the run. Engine stages run sequentially, but runtime RAM
still depends on model, context and engine settings. The checks summarized in
[verification](verification.md) establish no RAM minimum or speed guarantee.

## Docker

Build from the Media to Subs repository:

```sh
docker build --target api -t media-to-subs:api .
docker run --rm media-to-subs:api --help
```

The default target is API-only and installs the built standalone wheel plus
FFmpeg/FFprobe. No models or local engines are installed in that target. It runs
as UID 10001 by default. The base-image tags, OS packages and pip dependency
resolution are not fully pinned; this is a local recipe, not a byte-reproducible
release image. `uv.lock` remains the Python development/test lock.

With keys already exported, input in `input/` and a
host-writable `output/` directory:

```sh
mkdir -p output
docker run --rm --read-only --tmpfs /tmp \
  --user "$(id -u):$(id -g)" \
  -e MEDIA_TO_SUBS_STT_API_KEY -e MEDIA_TO_SUBS_LLM_API_KEY \
  --mount "type=bind,src=$PWD/input,dst=/input,readonly" \
  --mount "type=bind,src=$PWD/examples,dst=/config,readonly" \
  --mount "type=bind,src=$PWD/output,dst=/output" \
  media-to-subs:api /input/input.mkv --config /config/api-api.toml \
  --output /output/result.zh.srt
```

An optional CPU target compiles pinned whisper.cpp and llama.cpp commits, using
at most two build jobs per engine. It never downloads model weights:

```sh
docker build --target local -t media-to-subs:local .
```

Use the same read-only input/config mounts and separate writable output, add
`--mount "type=bind,src=$PWD/models,dst=/models,readonly"`, select
`/config/local-local.toml`, and use `media-to-subs:local`. For fully local managed
inference add `--network none` and omit both key environment options. The profile's
`../models` paths resolve to `/models` in this layout. An existing endpoint must
be in the same container's loopback namespace; `127.0.0.1` is not the host machine.
Do model setup outside the container first, with a separate writable model directory.
Mount only the required input, configuration, model and output directories.

## Troubleshooting

| Failure | Check |
| --- | --- |
| Missing FFmpeg/FFprobe | Install both and confirm PATH or configure their binary fields. |
| Local model/binary missing | Check the selected stage and its paths; downloading a GGUF does not install llama-server. |
| Bad timestamps | Select a timestamp-capable API/model; plain text cannot be safely turned into timed SRT here. |
| Wrong language/audio | Set `source_language` and the absolute `audio_stream` explicitly. |
| Local server load timeout | Check model format, RAM and `startup_timeout`; use a smaller model explicitly if needed. |
| Port in use | Choose a free loopback port or deliberately use the existing-endpoint profile. |
| HTTP 401/403 | Check the selected stage's key and endpoint; do not reuse production secrets implicitly. |
| Model checksum mismatch | Preserve/inspect the file, then move it aside and retry the pinned download. |
| Existing SRT or write permission error | Choose a distinct writable output path; use overwrite only intentionally. |

## Verification status

See [the complete verification record](verification.md). The original pipeline baseline passed 185 tests on both native macOS/arm64 and
Linux/arm64 containers. The install/setup verification is recorded separately.
Both API and local images built in that original baseline; the local image transcribed and translated the included speech fixture
with `--network none`. An independently installed wheel also passed API/API
fixtures with no local inference engines on PATH and no other project dependencies.

The downloader's 16 tests cover cache integrity, resume/restart, corrupted or
truncated responses, competing writers, locking, timeout and link hazards. All
five TOML profiles load, and all three reference model hashes match the manifest.
The container builds pinned CPU engine commits recorded in [provenance](provenance.md).
Latest installer/agent changes still lack fresh Linux verification.

The included [Japanese FLEURS fixture](../examples/README.md) has explicit
attribution. Korean real speech, hosted APIs, Windows, Linux/amd64, and broad GPU
compatibility remain unverified. Model weights and private generated speech are
excluded from the package.


## Agent translation

With transcription already configured, `media-to-subs setup --llm agent --agent codex`
saves Codex translation without changing the speech backend. For a one-run override,
use `media-to-subs movie.mkv --llm agent --agent codex`. Other choices are `claude`,
`opencode` and `agy`. Install and sign in to each CLI using its own supported workflow;
Media to Subs never installs a CLI, extracts its tokens, or shares login credentials
between providers. It requires no LLM model download in agent mode.

Optional flags: `--agent-model MODEL`, `--agent-binary /absolute/executable`, and
`--agent-timeout SECONDS` (300 by default, at most 3600 per request). Model selection
is explicit or the CLI default; Codex excludes user configuration, including its
configured default model. When switching agents, supply compatible model/binary
overrides or rerun setup from another LLM mode to clear old overrides.

Supported capability baseline: Codex 0.166.0+ within 0.x; OpenCode V2 2.2.5+ within
2.x. Claude must advertise all required print/safe-mode/output flags. The command
checks help/version before submitting text and fails on unsupported capabilities.
Doctor only checks executable availability; it does not verify login, quota or
subscription coverage. Claude has documentation-derived fixture coverage only;
installed capability and live acceptance remain unverified. OpenCode has installed
capability and fixture coverage; successful live translation remains unverified.
See [verification](verification.md).

Antigravity uses exactly the inspected CLI 1.4.12 and disables CLI auto-updates
for every invocation. For an explicit session model:

```sh
media-to-subs movie.mkv --llm agent --agent agy --agent-model gemini-3.8-flash-medium
```

It selects a private custom agent with no tools or inherited customizations,
strict permissions and an explicitly attached deny-all tool hook. The CLI's init
tool inventory is registry metadata; any actual tool event still rejects the
response. Offline replay, independent tool-denial canaries and an installed
Japanese-audio-to-Chinese-SRT run passed on macOS arm64 with Gemini Flash Medium.
This is historical evidence, not a fresh release-candidate check.
See the [Antigravity contract](antigravity-translation-contract.md).

Each subtitle batch launches a fresh CLI request. Startup and hosted inference can
make long files slower than a persistent local model server. The selected CLI owns
its internal transport retries and account limits; this application does not fall
back to another provider or retry auth/quota/process failures. One correction request
is allowed for a successful response with invalid subtitle content.

Agent mode uses the selected CLI's provider route, which may be cloud-hosted or
API-billed. Existing subscription access depends on that CLI/account/model; it is
not promised for every login. See [privacy](privacy.md#installed-agent-clis) and the
[adapter contract](agent-translation-contract.md) for exact restrictions.
