# Installed agent translation contract

This is the source/test handoff for the bounded agent translation assignment. Product documentation, installation, authenticated inference and plan acceptance belong to the coordinator. No live inference was performed by the implementation worker.

## Configuration and user commands

STT remains `local|api`. LLM adds `agent` without changing either default or the version-1 saved-profile schema. Existing local/API profiles continue to load.

| Field in `[llm]` / saved `settings.llm` | CLI and setup flag | Default / validation |
| --- | --- | --- |
| `mode` | `--llm` | `local`; accepts `local`, `api`, `agent` |
| `agent` | `--agent` | unset; required in agent mode: `codex`, `claude`, `opencode` |
| `agent_model` | `--agent-model` | unset; optional explicit CLI model override |
| `agent_binary` | `--agent-binary` | unset; provider executable name on PATH, or one explicit executable path |
| `agent_timeout` | `--agent-timeout` | 300 seconds; finite, positive, at most 3600 |

Run and doctor use flags > explicit TOML > `MEDIA_TO_SUBS_LLM_<FIELD>` > intentional saved profile > defaults. An explicit TOML or `--no-profile` bypasses saved settings/keys as before. Setup saves explicit flags and interactive selections; it retains its existing behavior of not importing arbitrary configuration environment fields. Setup's key prompts/environment apply only to API stages. No command-template or shell-string option exists. Optional model/binary fields follow fieldwise precedence; when changing providers, supply compatible overrides or rerun setup from a different LLM mode to clear prior fields.

Example explicit invocation:

```sh
media-to-subs movie.mkv --llm agent --agent codex
media-to-subs doctor --llm agent --agent opencode
media-to-subs setup --llm agent --agent codex
```

Setup still needs a complete STT choice. Agent mode needs no LLM weights, server, API model or endpoint. It clears effective `url`, `model`, `model_path`, `managed_server`, and `api_key`. Saving an agent profile rejects stale endpoint/weight settings and any LLM credential. Switching setup from API/local constructs new LLM settings and drops its old saved LLM key. STT settings and credentials remain independent. No agent tokens are saved or copied by Media to Subs.

Doctor checks executable availability only, labels login/quota untested, and neither starts an engine nor performs inference. Setup calls that same check. Before the first translation batch, the adapter runs bounded help/version probes in private scratch; missing capabilities fail before subtitle submission. It never installs or upgrades a CLI.

## Shared translation and process boundary

`translator.translate` still owns `build_messages`, batching, neighboring text context, relevant glossary filtering, `parse_translations`, and the existing single correction retry. CLI stdin receives a JSON serialization of the two existing system/user messages. Subtitle entries contain only `id` and `text`; no timestamps, media bytes, or provenance paths are sent. Existing explicit media-context opt-in is preserved. Original IDs, sequence, count, timings and authored overlap policy are reconstructed from the source after validation. Pipeline validation and atomic publication are unchanged; the chosen agent executable joins protected inputs.

Each batch or correction gets a fresh mode-0700 directory under `/tmp`, removed on exit. Owned schema/config/output files start mode 0600. Prompt data travels only over stdin; no prompt is placed in argv. Arguments are a list with `shell=False`. The executable resolves before changing cwd. The adapter is supported on POSIX macOS/Linux; it rejects other OSes before launching.

An environment allowlist retains `HOME`, `PATH`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `SSL_CERT_FILE`, and `SSL_CERT_DIR` when present. It retains `CODEX_HOME` only for Codex, and `CLAUDE_CONFIG_DIR` only for Claude. `PWD` and `TMPDIR` point to scratch; `NO_COLOR=1`. All other inherited variables are dropped, including `MEDIA_TO_SUBS_*`, API keys/auth tokens, other harness homes, node/python startup injection, session/server overrides and OpenCode config injection. CLI-managed login stores are accessed by the selected CLI itself; this application does not inspect their contents.

The timeout is a total deadline for that request, including capability probes on the first batch. Each probe also has a 10-second limit. Cancellation and timeout terminate the invocation's owned process group and reap its leader. Descendants must remain in that group; the application does not discover or stop processes by name/port. Cleanup can add the existing runtime's small grace period. It never resumes sessions, attaches a shared agent service, or changes service configuration.

`run_process` adds optional `max_output_bytes`; omitted calls retain prior behavior. The agent path caps combined stdout/stderr at 2 MiB, probes at 128 KiB, stdin prompt at 1 MiB, and Codex final-file reads at 1 MiB. Nonblocking POSIX pipe I/O enforces the capture cap while the process runs. Final-file reads reject symlinks, nonregular files and hardlinks. Raw child stdout/stderr is never included in user-facing errors.

Process failures, quota/auth failures and invalid/failed/incomplete provider envelopes do not trigger an application retry or provider fallback. Only a successful envelope whose subtitle content fails existing translation validation can trigger one correction. The selected CLI still owns its internal transport behavior and hosted-provider account limits. No subscription compatibility or absence of API billing is inferred from selecting a CLI.

Provenance records `engine` as the selected CLI, `mode="agent"`, and `model_source="configured"` with the configured override, or `model_source="cli-default"` with `model=null`. It does not invent an observed model name. Existing secret redaction applies to configured provenance.

## Codex

Minimum accepted release: **0.166.0**, major version 0, plus required `exec --help` capability flags. Locally verified: `codex-cli 0.166.0`. Exact command before an optional `--model MODEL` and final stdin `-`:

```text
codex exec
  --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check
  --sandbox read-only --json --color never
  -c approval_policy="never"
  -c project_doc_max_bytes=0
  -c web_search="disabled"
  -c suppress_unstable_features_warning=true
  --output-schema SCRATCH/schema.json
  --output-last-message SCRATCH/response.json
  --disable shell_tool --disable apps --disable plugins --disable hooks
  --disable multi_agent --disable browser_use --disable view_image
  --disable skill_search --enable skip_host_skill_discovery
  [--model MODEL] -
```

The schema is the existing `{translations: [{id: string, text: string}]}` shape with required fields and no extra properties. Ignoring user configuration still uses the CLI's auth store. An omitted model uses Codex's default with user config excluded. System-managed policy still applies. There is no permission/sandbox bypass flag.

JSONL must begin with `thread.started`, include one `turn.started`, exactly one completed `agent_message`, and end with `turn.completed`. Reasoning items are allowed; command/tool/MCP/web events, unknown events, warning/error items and failed turns are rejected. The completed message must match the bounded final-file text. In particular, the coordinator's initial probe showed an unstable-feature warning for `skip_host_skill_discovery`; per-invocation suppression prevents it, and the parser does **not** broadly forgive error items.

Evidence: installed help and the coordinator-supplied synthetic probe; general event/output behavior is described in [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive/). The earlier coordinator probe reported successful CLI-login inference with a normal terminal event and no tools; the implementation worker did not duplicate it. The subsequent installed audio-to-SRT check is summarized in [verification](verification.md). These are historical results, not fresh release-candidate acceptance.

## Claude Code

Claude is absent locally. No installation or inference was attempted. Compatibility is gated by bounded `claude --help` advertising every used flag; there is no speculative numerical minimum. Exact command:

```text
claude --print --output-format stream-json --verbose
  --tools '' --disallowedTools '*'
  --safe-mode --setting-sources ''
  --strict-mcp-config --mcp-config SCRATCH/mcp.json
  --no-session-persistence --max-turns 1 [--model MODEL]
```

The MCP file is `{"mcpServers":{}}`. Safe mode disables user/project customizations while retaining CLI authentication; managed policy remains authoritative. See the official [CLI reference](https://code.claude.com/docs/en/cli-reference) and [programmatic usage](https://code.claude.com/docs/en/headless).

The JSONL parser accepts initialization and assistant text/thinking blocks, then one terminal `result` with `subtype="success"`, `is_error=false`, nonempty string `result`, a normal/absent stop reason and completed/absent terminal reason. API errors, permission denials, deferred tools, tool-use/result blocks, unknown events and abnormal termination are rejected. The final text goes through `parse_translations`.

**Deliberate protocol choice:** do not pass `--json-schema`. The official [structured-output mechanism](https://code.claude.com/docs/en/agent-sdk/structured-outputs#recognize-the-structuredoutput-tool-call) introduces a `StructuredOutput` tool and can reprompt internally. That conflicts with this assignment's tool rejection and single correction policy. Prompted JSON plus the existing strict validator preserves those requirements. Claude proof is documentation-derived fixtures only; installed flag compatibility and real authentication/output behavior remain unverified.

## OpenCode V2

Minimum accepted release: **2.2.5**, major version 2, plus `run --help` capabilities. Policy controls used here require 2.2.3 or later; 2.2.5 is the stricter protocol baseline inspected for this implementation. Locally verified: `opencode v2.2.5`. V1 is rejected. Exact command:

```text
opencode run --standalone --format json --agent media-to-subs-UNIQUE
  [--model provider/model#variant]
```

No `--auto`, `--server`, session/continue/fork argument, or service mutation is used. `--standalone` owns a private server; upstream gives its server a stdin ownership lease. Source confirms stdin prompt consumption and structured output: [V2.2.5 run.ts](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/run/run.ts), [noninteractive.ts](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/run/noninteractive.ts), and [standalone.ts](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/services/standalone.ts). See also [CLI documentation](https://opencode.ai/v2/docs/cli/).

The private `opencode.jsonc` contains:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "share": "disabled",
  "snapshots": false,
  "agents": {
    "media-to-subs-UNIQUE": {
      "mode": "primary",
      "system": "THE EXISTING TRANSLATION SYSTEM MESSAGE",
      "steps": 1,
      "permissions": [{"action": "*", "resource": "*", "effect": "deny"}]
    }
  },
  "policies": [
    {"action": "tool.use", "resource": "*", "effect": "deny"},
    {"action": "mcp.use", "resource": "*", "effect": "deny"},
    {"action": "skill.use", "resource": "*", "effect": "deny"},
    {"action": "plugin.use", "resource": "*", "effect": "deny"}
  ],
  "plugins": ["-opencode.config.instruction", "-opencode.config.compatibility"]
}
```

A fresh random agent avoids inherited named-agent tool grants. Agent-specific deny rules follow global permission rules. The instruction/compatibility plugins are disabled using documented plugin controls and source-verified IDs. Provider/login configuration is left with OpenCode; it is never parsed or copied by this application. See [configuration](https://opencode.ai/v2/docs/config/), [agents](https://opencode.ai/v2/docs/agents/), [policies](https://opencode.ai/v2/docs/policies/), [plugin controls](https://opencode.ai/v2/docs/plugins/#control), [instruction plugin source](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/config/plugin/instruction.ts), and [compatibility plugin source](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/config/plugin/compatibility.ts).

JSONL must contain a single `step_start` / `step_finish` pair for the same session and assistant message, with terminal reason `stop` and nonempty concatenated `text` parts. Reasoning parts are allowed. `tool_use`, errors, additional steps, unexpected event/part types, cross-session data and abnormal finishes fail. Text parts can follow the finish event because upstream reconciliation may emit unreported text after the live step ends.

OpenCode's broader user/system/Console policies can override narrower policy entries, and managed settings can override project settings. These restrictions are **not OS isolation**, nor a promise that arbitrary configured plugins cannot run. Private-server use does not imply ephemeral session storage: OpenCode may retain the session in its own database. The application does not delete that database or existing sessions. Evidence covers installed help/version, source and synthetic protocol fixtures; see [verification](verification.md). Successful OpenCode inference, subscription coverage, and effective restrictions with a successful real response remain unverified.

## Verification boundary

Tests exercise all three real fake-executable boundaries, stdin/argv/cwd/environment, fresh-directory cleanup, ID/timing/glossary/context preservation, correction exhaustion, error/tool/incomplete responses, unsupported capabilities, timeout/cancellation/output flood, safe final-file matching, profile key dropping, and protected publication. Shared runtime tests cover descendant cleanup while preserving an unrelated process. Full unittest and build results are recorded in the assignment result.

The coordinator owns final product docs, current-user installation/profile-byte preservation, any further authenticated checks, review/integration and plan completion. No user profile, credentials, other worktree, service configuration, model installation or remote repository was modified by this worker.
