# Media to Subs

A standalone Python CLI for one local media file → timed source transcript →
Chinese SRT. Speech transcription (STT) and translation (LLM) independently use
local engines or explicitly configured APIs. Translation also supports installed
Codex, Claude Code and OpenCode CLIs. No web app or database is required.

**Experimental v0.1; feature scope frozen.** The core pipeline has local and
short-sample end-to-end evidence. Hosted APIs, live Claude/OpenCode success,
Korean real speech and long-form quality remain unverified. Review the
[support evidence](docs/verification.md) before choosing a backend.
The project code is [MIT licensed](LICENSE). The attributed speech fixture
retains its separate CC BY 4.0 license; see [licensing](docs/licensing.md).

## Install once

On macOS or Linux, install Python 3.11+ with its `venv`/`pip` support, then run
from this repository:

```sh
./install.sh
media-to-subs setup
```

The installer creates an isolated installation for your user. You do not need
`uv`, virtual-environment activation, or administrator access to install the command.
If your user command directory is not on PATH, the installer prints the required
PATH instruction. Setup checks the additional tools needed by your chosen modes.

Setup asks whether transcription should run locally or through an API, and whether
translation should use a local model, an API, or an installed agent CLI. Choose existing model files or explicitly download the pinned reference
models. For API stages, enter the endpoint, model and credentials once. Choices
are saved in your private user profile; rerun `media-to-subs setup` to change them.

## Run

From any directory:

```sh
media-to-subs video.mkv
```

This writes `video.zh.srt` beside the input. Suitable existing subtitles are reused;
otherwise the saved transcription and translation backends are used. Media and
authored sidecars are preserved. Existing output is protected.

```sh
media-to-subs doctor                   # Check your saved setup
media-to-subs video.mkv --output out.srt
media-to-subs video.mkv --save-source source.srt
media-to-subs --help
```

Both stages support local or API execution independently. Local mode never silently
switches to a paid provider. API usage may incur charges. Local inference needs
FFmpeg/FFprobe and the selected Whisper/LLM engines. See [setup](docs/setup.md)
for platform prerequisites, installation details and model storage.

API credentials saved by setup are stored in a private local file protected by
filesystem permissions; they are not encrypted. Keys are separate for each stage
and bound to its saved endpoint. They are never command-line arguments. See
[credential handling](docs/privacy.md#credentials-and-context).

## Use your coding-agent login for translation

Install and sign into the selected CLI separately, then try it for one run:

```sh
media-to-subs video.mkv --llm agent --agent codex
```

Whisper still transcribes the audio. The agent translates cue text; the script
keeps control of timestamps, validation and output files. No local LLM weights
are needed for this mode. To save the choice for future plain commands:

```sh
media-to-subs setup --llm agent --agent codex
media-to-subs video.mkv
```

Choose `claude`, `opencode` or `agy` instead of `codex` for the other adapters.
Antigravity requires the inspected CLI 1.4.12. Its installed Japanese-audio-to-Chinese-SRT
path is verified with `--agent-model gemini-3.8-flash-medium`. See the [Antigravity contract](docs/antigravity-translation-contract.md).
`--agent-model MODEL` explicitly selects a model; otherwise the CLI default applies
(Codex runs with user configuration excluded). Your selected CLI's authentication,
provider route and usage limits apply; a CLI login does not guarantee subscription
coverage. Subtitle text may go to its hosted provider. See [agent setup](docs/setup.md#agent-translation)
for compatibility, privacy and verification limits.

## Advanced configuration

You can still supply an explicit TOML file or per-run flags. An explicit `--config`
bypasses the saved profile and saved keys; use separate STT/LLM environment keys
for that invocation. `--no-profile` disables saved defaults entirely.

```sh
media-to-subs video.mkv --config examples/local-local.toml
media-to-subs video.mkv --config examples/local-api.toml
media-to-subs video.mkv --config examples/api-local.toml
media-to-subs video.mkv --config examples/api-api.toml
```

Relative model paths in TOML resolve against the config file. For a local server
you already run, setup can use its loopback endpoint without owning its process;
[local-existing.toml](examples/local-existing.toml) shows the advanced equivalent.

## v0.1 boundaries

- Japanese/Korean speech; Japanese/Korean/English authored subtitles; Chinese
  output. Generated translations target Simplified Chinese. Suitable existing
  Chinese subtitles are preserved as-is. Broader language support is unclaimed.
- Suitable sidecar/embedded subtitles can avoid transcription; suitable Chinese
  can avoid both engines. `--force-transcribe` deliberately requests transcription.
- Usable timestamps are mandatory. Plain transcription text is insufficient.
  Missing, nonfinite, inverted or out-of-range timings fail; the LLM translates
  cue text and cannot invent or rewrite timings. Inferred cues must not overlap;
  authored overlap support is explicit and preserves the source policy.
- Inputs and sidecars are preserved. Output is published only after validation.
  Existing output is protected unless `--overwrite` is explicit; input/output
  aliases and symlink hazards remain invalid. `--save-source` exports source SRT
  under the same protection rules.
- `--include-media-context` is an explicit opt-in: translation receives the media
  basename/title and immediate parent folder name. Default behavior sends no
  path/title context. See [privacy](docs/privacy.md).
- One local input per run. No remote media URL, recursive queue, dashboard,
  persistent application database, subtitle search, or arbitrary target language.
- No universal model quality, real-time speed, Windows-native, GPU, or multi-platform
  container promise. Model hash checks and HTTP fixtures do not prove inference
  quality. See the precise [evidence boundaries](docs/setup.md#verification-status).

## More

- [Setup, Docker, model pins and troubleshooting](docs/setup.md)
- [Data flow, credentials and file protection](docs/privacy.md)
- [Licensing and publication checklist](docs/licensing.md)
- [Dependencies and asset provenance](docs/provenance.md)
- [Verification and support boundaries](docs/verification.md)
- [Changelog](CHANGELOG.md)
- [Contributing and local checks](CONTRIBUTING.md)
