# LLM translation

`media_to_subs.translator.translate(transcript, config, glossary, cancellation,
progress, *, media_context=None)` returns a `TimedTranscript`. It sends sequential
OpenAI-compatible `/chat/completions` requests and returns only after every cue
passes validation. It does not read or write subtitle/media files. The pipeline
owns output publication and must finish STT before requesting a managed LLM.

Silence and a `zh` language tag (including `zh-Hans`/`zh-CN`) return the original
transcript without checking a model, server binary, API URL or key. Cancellation
is still checked. Other inputs preserve exact IDs, supplied order, start/end
times and the explicit authored-overlap policy. Output language becomes `zh`;
existing provenance is retained with a `translation` entry describing the engine,
mode, selected model, source language, batch size and managed-server choice.

## Explicit routes

- Local mode accepts only loopback addresses or `localhost`. `localhost` is pinned
  to `127.0.0.1`, so use `[::1]` explicitly for an IPv6-only listener. The unset URL
  defaults to `http://127.0.0.1:8080/v1`. A user-managed server is never stopped.
- API mode requires an explicit URL and model. The LLM key is independent of the
  STT key; programmatic `LLMConfig.api_key` or configuration loader's
  `MEDIA_TO_SUBS_LLM_API_KEY` is the only selected credential. A key is optional for
  explicitly unauthenticated compatible endpoints. Nothing reads the source
  application's settings, `.env`, generic provider keys or netrc credentials.
- The URL is a full base URL, normally ending in `/v1`; `/chat/completions` is
  appended exactly once. An already complete chat-completions URL is accepted.
  URLs with userinfo/query/fragment are rejected by configuration. No URL,
  authorization header or provider error body appears in diagnostics.
- No route, provider, model, transport API, or unstructured-output fallback is
  attempted. The endpoint must support nonstreaming chat completions with
  `response_format={"type":"json_object"}`, `temperature=0` and `max_tokens=1024`.

## Content and validation

Batches contain up to `batch_size` cues and a conservative 1,200-character source
budget (including estimated JSON overhead). A single longer cue stays intact;
it is never silently split or truncated. One neighboring source cue on each side,
clipped to 160 characters each, supplies dialogue context. Only glossary terms
matching the current source cues enter the prompt. Model context exhaustion or
truncation fails validation; use smaller batches or a user-managed server with a
larger context for long cues/glossaries. This is a size bound, not tokenizer-based
packing or a translation-quality guarantee.

The system instruction labels subtitles, glossary and context as untrusted data.
All variable text is serialized into the user JSON message, never interpolated
into system instructions. No content path, filename or folder is inferred from
provenance or the filesystem. `media_context` is an optional explicit string from
the caller, included under its own untrusted JSON key only when supplied. The CLI
may populate it after `--include-media-context`; default `None` transmits none.
It is never added to diagnostics or provenance. This separation reduces prompt
injection exposure; structural validation cannot prove semantic obedience.

Responses must contain exactly one `translations` array with one `{id, text}`
object for each requested string ID. Fenced JSON and prose wrapped around one
valid JSON object are recovered. Missing, duplicate, extra or coerced IDs,
duplicate JSON keys, malformed JSON, unsupported fields, blank text, control
characters and blank lines within cues are rejected. Provider response order may
differ: validated results are assembled in the original source order. ID spelling
(including leading zeroes) never changes.

Ordinary translated cues must contain Han text and no Japanese kana or Korean
Hangul. Numeric/punctuation-only cues may be preserved exactly. An explicit
glossary mapping covering the entire source cue may require a Latin proper noun
instead of Han text. These exceptions do not allow ordinary unchanged English,
Japanese or Korean prose. Han detection is a conservative script check, not proof
of Simplified Chinese, fluency, accuracy or completeness of meaning.

`read_glossary(path)` reads only an explicit UTF-8 (optional BOM) JSON file up to
1 MiB, containing a source-to-target string object, for example:

```json
{"ExampleCo": "示例公司", "cat": "小猫"}
```

`validate_glossary(mapping)` returns a validated copy; `None` means no mappings.
Duplicate JSON keys, nonstrings, empty entries, surrounding whitespace and control
characters fail without echoing terms/paths. Matching is case-sensitive literal
substring matching within each source cue; every matching target must occur
literally in that cue's translation. Conflicting/overlapping glossary requirements
can therefore make translation fail. No glossary discovery, merging or automatic
path-derived context occurs.

## Bounds, retries and cancellation

Requests run in a small private Python process using the installed `requests`
dependency. Credentials/request data pass over stdin, never argv or temporary
files. Both local and API sessions use `trust_env=False` and disallow redirects.
No proxy, netrc or provider environment fallback is used. TLS verification remains
enabled. Successful response bodies are capped at 2 MiB; error/redirect bodies
are not consumed or reported.

Each attempt uses explicit connect/read timeouts and a total wall-clock budget of
`connect_timeout + read_timeout`, including helper startup. Thus a server that
continually sends a few bytes cannot extend a request forever. Cancellation and
the total deadline terminate the owned HTTP process group, with the runtime's
bounded cleanup grace. They cannot cancel work already accepted by a remote
provider. No request thread remains running in the parent.

Only HTTP 408, 425, 429, 500, 502, 503 and 504 permit up to `max_retries` additional
attempts, with interruptible exponential delays capped at four seconds. Auth,
other client/server errors, redirects, connection failures and timeouts do not
retry. Separately, a successful response with invalid JSON/content/IDs/glossary or
an unfinished completion permits **one** correction request for the same batch.
Rejected output is not fed back as instructions. Each correction has the same
transport budget, so the maximum requests per batch is `2 * (max_retries + 1)`.

A transient status can arrive after the provider did some work; retrying may
perform or bill duplicate work. There is no provider-independent idempotency
promise. Connection/read timeouts are particularly ambiguous and are not retried
automatically. Validation correction deliberately requests fresh work. No paid
requests were used in this slice's tests.

## Optional managed llama-server

Set `managed_server=True`, local `model_path`, and optionally `server_binary`,
`cpu_threads` and the loopback URL. Managed mode requires an HTTP `/v1` (or full
`/v1/chat/completions`) endpoint. The adapter validates only these needed assets,
checks that the port can be bound, then launches an owned process group with:

```text
--host <loopback> --port <port> --alias <random per-launch identity>
--ctx-size 2048 --threads <cpu_threads> --gpu-layers 0 --parallel 1
--offline --no-webui --model <explicit local GGUF>
```

The selected GGUF is never downloaded or packaged. Other inherited llama/provider
configuration is excluded. An optional key is supplied via `LLAMA_API_KEY` in the
child environment, not an argument or log. Server stdout/stderr are discarded.
Readiness requires `/health` status `ok`, a matching unique alias from `/v1/models`,
and a live owned process. Startup is bounded by `startup_timeout`; readiness
probes use short cancellable deadlines. The unique alias detects another server
winning the bind race after the preflight socket is released. It is a process
ownership check, not authentication against a malicious local user.

Only the launched process group is cleaned up after success, failure or
cancellation. A preexisting listener causes an error and is never probed for
termination, reused as owned, or killed. User-managed servers have no lifecycle
ownership. POSIX process-group cleanup is tested on macOS; Windows descendant
cleanup is not claimed. Servers must not daemonize or escape their process group.

## Verification

Translation uses JSON-wrapper recovery, synchronous batching, required-term
checks and strict cue-ID coverage. Configuration is explicit, HTTP requests are
cancellable, and managed serving has bounded ownership. Fixtures use synthetic text.

Run `uv run --frozen python -m unittest discover -s tests -v`. The LLM suites use
real localhost HTTP and executable process fixtures for malformed responses,
coverage errors, auth/transient errors, redirects, body bounds, cancellation,
startup failure, wrong readiness identity, port conflicts and listener survival.
The installed llama-server 0.7.0 build 11669 (620d2225c) and the explicitly supplied
Qwen2.5 1.5B Instruct GGUF passed a managed two-cue synthetic translation in
16.53 seconds, preserving IDs/times and the required glossary target; the listener
was released afterward. This used CPU threads 4, context 2048 and GPU layers 0. The full
STT/LLM mode matrix, paid API behavior, subtitle quality and production acceptance
remain coordinator-level checks.
