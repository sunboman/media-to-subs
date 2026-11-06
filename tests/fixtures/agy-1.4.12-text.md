# Antigravity 1.4.12 text response fixture

`agy-1.4.12-text.jsonl` is a normalized regression fixture derived from the
observed CLI 1.4.12 event shape, not an original execution capture. Conversation
ID, working directory and agent name use `fixture-session`, `/scratch` and
`translator`. Optional `usage` and `duration_seconds` fields are omitted.
The model label, 60-entry shared registry, strict permission mode, event order,
text chunks and final response are retained for protocol coverage. The dialogue
is synthetic, not an episode excerpt. No credentials or user profile data are
included.

The represented event shape failed the old empty-inventory check. Inspection
established that the init emitter uses a shared registry, not the selected
agent's effective tools. See [the adapter contract](../../docs/antigravity-translation-contract.md)
for that evidence and the separate exclusion/denial canaries. Replay checks
parsing and rejection behavior without optional telemetry; it does not prove
live inference or tool denial.
