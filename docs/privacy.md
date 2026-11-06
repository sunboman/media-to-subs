# Privacy and file handling

The CLI has two independently configured stages. The data flows below are
covered by process/HTTP fixtures and real local-engine checks. See
[verification](verification.md) for the tested platforms and remaining hosted
provider checks.

| STT | LLM | What leaves the machine when transcription/translation are needed |
| --- | --- | --- |
| local | local | No media or transcript sent to a hosted provider. A user-managed local endpoint still receives text over loopback. |
| local | API | Source cue text, translation instructions and any supplied glossary go to the configured LLM endpoint. |
| API | local | Decoded audio chunks go to the configured STT endpoint. Source cue text goes to the local LLM endpoint. |
| API | API | Audio goes to STT; source cue text, instructions and supplied glossary go to LLM. |
| local or API | agent | Cue text, instructions, neighboring text and supplied glossary go to the selected CLI and potentially its hosted provider. API STT still receives audio separately. |

The STT and LLM endpoints, model choices and key environment variables are
separate. Choosing local must never silently select an API on failure. API provider
retention, logging, billing and geographic handling depend on that provider; this
CLI cannot make a provider's service offline. A compatible protocol does not
certify a provider's privacy policy or model quality.

## Explicit setup versus inference

Choosing a model download in `media-to-subs setup`, or explicitly running the
model downloader, deliberately contacts the pinned model host
and any HTTPS download redirects. That host can observe the request and network
address. The script sends no media and needs no provider credentials. It accepts
public HTTPS model URLs; HTTP is restricted to literal loopback addresses for
local testing. Python's normal proxy and certificate environment may affect the
connection. No network request is made by `--list` or a valid cache hit.

Model downloads never happen during package installation. Installing Python
packages, pulling/building Docker images and installing engine binaries normally
require network access. Prepare those assets first; running with installed local
assets is the offline inference case. A container can additionally use
`--network none` for local mode, including a managed loopback llama-server.

## Credentials and context

- Setup reads keys with a hidden prompt and saves them separately for STT and LLM
  in `~/.config/media-to-subs/profile.json` (or under `XDG_CONFIG_HOME`). The folder
  is private to your user and the file has owner-only permissions. Keys are stored
  as plaintext, not encrypted; local backups may contain them. Never commit or
  share the profile. Environment keys remain available for per-run use.
- Saved keys are bound to their configured request endpoints. Changing an endpoint
  for a run does not forward its old saved key. Reconfigure with setup or supply
  the separate explicit environment key. Local mode does not reuse saved API keys.
- Normal processing reads only the intentional user profile. `--config` replaces
  it for that invocation, including bypassing saved keys; `--no-profile` disables
  it. No implicit `.env` or configuration from the current directory is loaded.
- Never put key values in CLI arguments, example TOML, committed files or logs.
  Setup and doctor do not print stored keys.
- `--include-media-context` (or `[run].include_media_context = true`) opts in
  to sending the media basename/title and immediate parent folder name to the
  translation stage only. It defaults to false: no path/title context is sent.
  The opt-in does not send an absolute path or ancestor directory names.
  A glossary can contain private names and is included in translation requests.
- Error handling must not print authorization headers, secret URL fields or
  untrusted provider response bodies. Sanitized errors can still disclose local
  filenames; treat diagnostic logs as private where filenames are sensitive.
- The CLI owns only processes it starts. An existing local endpoint keeps its own
  logging/privacy behavior and must survive CLI shutdown or failure.

## File protection contract

Media and authored sidecars are preserved. Final SRT and optional source-SRT
exports require valid cues and timestamps before publication. Existing output is
rejected without `--overwrite`, including a file created while processing. With
explicit overwrite, the previous complete file remains until the validated
replacement is ready. Input/output aliases and symlink hazards must be rejected.
Only run-owned scratch files may be cleaned up.

The model setup script has its own tested protection: valid final files are
rehash-validated; invalid final files are preserved and cause failure. It never
overwrites a final model. `.part` content may be resumed or restarted when the
server ignores Range, then published atomically only after size/SHA256 checks.
Interrupted or corrupt partial files remain available for inspection; remove or
move a corrupt partial yourself before starting fresh. Locks are advisory OS locks
on persistent `.lock` files, released when the process exits.

Use a model directory you control on a local filesystem with hard-link support.
The script rejects symlink files and files with multiple hard links. It is not a
sandbox against another user who can rename/replace directories underneath it.
Do not store keys or unrelated files at the documented `.part` or `.lock` paths.


## Installed agent CLIs

Agent translation sends text and cue IDs over stdin, not media files or timestamps.
Media context remains opt-in. The selected CLI reads its own login store; Media to
Subs stores only the agent choice and optional model/binary/timeout. Unused LLM API
keys and model-server settings are dropped in this mode.

Requests run in fresh private temporary directories with bounded time/output, an
allowlisted environment and CLI-native restrictions on tools and customizations.
Codex uses read-only permissions; Claude disables tools; OpenCode uses a private
standalone server and deny policies. Antigravity selects an agent with no tools,
disables inherited customizations, and explicitly attaches its private deny-all
tool hook while retaining strict permissions. These controls are not OS isolation. Managed
settings and broader OpenCode configuration can override narrower restrictions.
Tool events and incomplete responses are rejected before output publication.

No existing agent session/server is resumed, changed or stopped. Only owned process
groups are cleaned up. Codex uses ephemeral mode and Claude disables session
persistence; OpenCode and Antigravity may retain text in their own session storage. Provider-side
retention and CLI-internal logging remain controlled by that provider/CLI. Refer to
the [adapter contract](agent-translation-contract.md) and
[Antigravity contract](antigravity-translation-contract.md) for tested versions and limits.
