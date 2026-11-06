# Antigravity translation contract

This adapter selects an installed `agy` CLI with `--llm agent --agent agy`.
`--agent-model gemini-3.8-flash-medium` supplies an explicit model slug; omitting
it leaves model selection to the CLI and records `cli-default` provenance.
The normal config/environment/profile precedence, setup persistence, and STT
selection stay independent. Doctor checks executable presence only; it does
not authenticate, run a model, or establish capability readiness.

## Recorded verification status

**Earlier installed end-to-end verification passed on macOS arm64.** The installed
command ran synthetic Japanese audio through Whisper small/Silero and explicit
`gemini-3.8-flash-medium` using CLI-managed login. It published one Chinese cue,
preserving its ID and exact source timestamps. Input audio, saved profile and CLI
settings remained unchanged. The strict parser accepted a complete text-only
response. This is one short synthetic sample, not broad translation-quality
coverage or a fresh check of the sanitized release candidate.

An earlier response was rejected because the validator required an empty
`init.tools` inventory. Inspection established that this field is a shared
registry, not the selected agent's effective tools. The normalized regression
fixture preserves the observed event shape and full inventory; replay succeeds
after correcting that assumption. It is not an original execution capture.

Coordinator differential canaries kept the same inventory while changing the
agent's callable tools: `tools: []` produced no tool steps, while explicitly
selecting `run_command` produced a real attempt denied by strict permissions.
These canaries support the binary evidence; a model's `NO_TOOL` answer alone
would not establish tool exclusion.

The canaries also found an independent guard-wiring defect. A discovered workspace
hook was not attached when `inheritCustomizations: false`; `hooks: [name]` did not
fix it. With the absolute hook **file path** explicitly selected, a synthetic
`view_file` attempt was denied by the pre-tool hook before returning file content.
This independent hook test used `request-review` and an explicit `view_file`
allowlist to avoid masking the result with strict permissions. Production keeps
`tools: []` and `toolPermission: strict`.

Earlier worker verification covered offline event replay, executable fixtures,
binary inspection and CLI-handled diagnostics. The protocol suite and package build passed. The coordinator separately reviewed
the changes, ran authenticated canaries, installed a matching wheel and verified
the audio-to-SRT path. No remote publication or deployment was performed. Final
release-candidate checks remain coordinator-owned.

## Inspected baseline and sources

The exact supported version is **1.4.12**, inspected on macOS arm64. No broader
release range is implied. This adapter never downloads or upgrades agy.
All its probes and translation invocations force
`AGY_CLI_DISABLE_AUTO_UPDATE=1`.

Primary sources used for the recorded compatibility review:

- [Headless mode](https://antigravity.google/docs/cli/headless): stdin NDJSON,
  EOF, stream event fields, schema behavior, model selection, statuses.
- [CLI reference](https://antigravity.google/docs/cli/reference): session
  selection, input/output, slash command, model, and timeout flags.
- [Custom subagents](https://antigravity.google/docs/subagents/): Markdown
  agent files, frontmatter, primary agent selection and execution policy.
- [Maintainer clarification on hook isolation](https://github.com/google-antigravity/antigravity-cli/issues/1261):
  `inheritCustomizations: false` and explicit `hooks:` paths; relative-path
  handling was fixed in 1.3.1. We use an absolute private file path.
- [Maintainer frontmatter reference](https://github.com/google-antigravity/antigravity-cli/issues/1270):
  supported fields include `hooks`, `tools` and both exclusion flags. A concrete
  model slug belongs in the session `--model` flag, not frontmatter.
- [CLI 1.2.1 release](https://github.com/google-antigravity/antigravity-cli/releases/tag/1.2.1):
  `excludeDefaultComponents: true` removes default prompts and built-in tools;
  post-invocation hooks remain. This is also in the inspected binary's embedded
  changelog. `tools: []` alone does **not** disable the default tools.
- [Official CLI changelog, 1.1.14](https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md#1114):
  `inheritCustomizations` controls inherited skills, rules, plugins, subagents,
  and MCP servers. The same entry is embedded in the inspected binary.
- [Hooks](https://antigravity.google/docs/hooks/): named workspace hooks,
  `PreToolUse`, wildcard matcher and hard denial. The built-in hooks reference
  specifies sequential hook merging, shell execution and the JSON deny result.
- [Customization discovery](https://antigravity.google/docs/customization-discovery/):
  workspace versus personal discovery and manifest limitations.
- [Settings schema](https://antigravity.google/schemas/v1/settings.json):
  strict permission mode, agent mode, non-workspace access and credit fallback.
  Installed 1.4.12 `--help` additionally documents `--settings` as a per-session
  JSON override that is never saved; this flag is required by the adapter.

## Installed init-emitter evidence

Read-only inspection of CLI 1.4.12 used disassembly and embedded Go function/line
mappings; it did not establish access to upstream source. `emitInit` calls
`buildInitPayload`, which obtains and sorts names from `advertisedTools` and reads
effective permissions separately. `advertisedTools` enumerates a shared registry
with backend/build filtering; it does not read the selected agent's tool
configuration. The registry is also used by tool formatting and plugin validation.

The headless documentation calls this inventory available tools, but it does not
describe effective per-agent availability in the inspected build. No direct
effective-tool readback was established. Neither `/hooks` discovery nor init
inventory proves runtime attachment or tool denial; the independent canaries
above establish the tested control behavior.

## Request boundary

Each request gets a new private directory under `/tmp`, outside the media
repository. Directories are mode 0700 and configuration files are mode 0600.
A unique `.agents/agents/media-to-subs-<random>.md` sets:

```yaml
excludeDefaultComponents: true
inheritCustomizations: false
hooks: ["/absolute/private/request/.agents/hooks.json"]
tools: []
mainAgent: true
subagent: false
inheritMcp: false
mcpServers: []
skills: []
plugins: []
commandExecutionPolicy: "off"
```

The body supplies the translation system instructions. The scratch
`settings.json` selects strict tool permissions, default agent mode, disables
non-workspace access and disables fallback to AI credits. These settings are
passed with `--settings`; user configuration is not edited.

A uniquely named `.agents/hooks.json` guard is enabled for `PreToolUse`, with
`matcher: "*"`. Its constant shell command prints a JSON `decision: "deny"`
with a fixed reason. It interpolates no subtitle, path, model or credential.
Before sending subtitle text, the adapter runs the CLI-handled `-p /hooks`
listing under the same agent and session settings and requires exactly one
matching enabled guard row, including the exact matcher and command.
Missing, disabled, duplicated or changed registration fails before inference.
The custom agent separately selects this exact absolute file in `hooks:`; the
list takes paths, not hook names. Disabling inherited customizations without that
selection leaves the hook discoverable but ineffective. Registration readback is
therefore only a discovery check. Explicit file selection plus the coordinator's
independent canary establishes the tested attachment; every request keeps the
same wiring and constant deny command.

The adapter sends exactly one line of JSON to stdin:
`{"event":"user","message":{"content":"<serialized system/user messages>"}}`,
then EOF. Arguments include `--input-format stream-json`,
`--output-format stream-json`, `--disable-slash-commands`, `--print-timeout`,
and `--model` only when configured. No subtitle or credential appears in argv.
It never passes a permission bypass, resumes a conversation, or invokes an SDK.

No `--json-schema` is passed. The coordinator's early schema probe returned
exit zero with four user turns and repeated array answers; the adapter rejects
that envelope. Schema enforcement may add internal correction turns and does
not replace application validation.

## Accepted response and failures

The parser requires one initial session, the expected working directory, unique
agent name and explicit model (when supplied), strict permission mode, and a
well-formed `init.tools` inventory (a list of unique nonblank strings). Inventory
names do not authorize tool calls, and no particular name, count or model is
special-cased. It then requires exactly one completed user-input
step, one completed agent-response step (possibly streamed in chunks), and one
terminal result. Session IDs must agree, step indices must be valid, and the
concatenated response deltas must equal the final text. The result must have
`status: SUCCESS` and integer `num_turns: 1`.

Unknown events/fields/step types, duplicate JSON keys, nonfinite JSON numbers,
errors, tools (including denied calls), subagents, incomplete steps, extra
turns, mismatched IDs/text, schema envelopes and early termination markers all
fail closed. Exit zero alone is insufficient. Raw provider output is never
included in errors. Auth/quota/process/envelope failures receive no application
retry. Only a successful envelope whose subtitle content fails validation may
receive the existing one fresh content-correction request. The original cue IDs,
order, timing, overlap policy, Chinese/glossary checks and publication validation
remain authoritative.

The existing monotonic request deadline includes capability and guard probes;
stdin, output limits, cancellation and owned POSIX process-group cleanup apply
unchanged. Probe output is capped at 128 KiB, translation output at 2 MiB and
serialized stdin at 1 MiB. The selected binary remains a protected input and
cannot be overwritten by subtitle publication.

## Limits and evidence boundary

These are **CLI-native controls, not OS containment**. The CLI retains access
to its own cached login and ordinary user storage. Media-to-Subs never reads or
copies credentials and does not inherit provider/API-key environment variables.
It does not edit the user's login, settings, plugins, services or saved profile.
A `/hooks` listing can include global definitions even when the selected agent
excludes inherited customizations; their appearance does not prove execution.
The agent explicitly loads only its private hook file. A deny hook governs model
tool calls, not arbitrary CLI or lifecycle code. User/managed policy and the CLI
implementation therefore remain trusted.

No supported ephemeral/no-persistence flag was established. Fresh requests do
not resume sessions, but CLI-owned history, logs or artifacts may persist under
the user's storage after the scratch directory is removed. The CLI's documented
internal transient API retries also remain its responsibility; this application
adds no retry for provider failures and cannot count hidden API attempts.

Worker verification uses synthetic fake executables for all inference paths,
real help/version and a CLI-handled `/hooks` readback for registration, plus
public/installed source inspection. It does not establish authenticated model
quality, live tool denial, end-to-end audio translation or Linux support.
Authenticated canaries, installation and final acceptance belong to the
coordinator. A broadened version/platform claim requires fresh evidence.
