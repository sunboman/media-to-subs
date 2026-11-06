# Verification and support boundaries

Media to Subs is an experimental single-file CLI. Structural correctness checks
protect timestamps, file publication and provider boundaries; they do not
establish universal transcription or translation quality.

## Inference coverage

| Path | Evidence | Limits |
| --- | --- | --- |
| Local Whisper + local LLM | Short Japanese-speech checks on macOS arm64; original Linux arm64 container checks | Small-model recognition/translation errors are known; recent installer/agent changes lack fresh Linux inference proof |
| Codex translation | Installed audio-to-SRT check using Codex 0.166.0 | Short synthetic Japanese sample; CLI/account compatibility and quotas still apply |
| Antigravity translation | Installed audio-to-SRT check with agy 1.4.12 and explicit Gemini Flash Medium | Exactly the inspected CLI version; short synthetic sample |
| OpenCode translation | Installed capability checks and synthetic protocol fixtures | Successful live inference and effective restrictions with a real response remain unverified |
| Claude translation | Documentation-derived protocol/executable fixtures | Installed capability and live acceptance remain unverified |
| STT/LLM HTTP APIs | Local HTTP fixtures and actual loopback engine servers | Hosted authentication/inference remains unverified |

Local/API transcription and translation were exercised independently in all four
combinations on the attributed [Japanese FLEURS sample](../examples/README.md).
Each output retained source cue IDs, order and timestamps within measured audio
length, and input bytes were preserved. These API checks used owned loopback
servers rather than hosted providers. Native Whisper may use Metal; the checks
are not CPU-only or comparative performance benchmarks.

The [Antigravity contract](antigravity-translation-contract.md) explains why the
reported tool registry is metadata, how the explicit deny hook is attached, and
which independent canaries support the tested control behavior. Fixture replay
alone does not prove live tool denial or authenticated inference.

## Automated and installation checks

The documented unittest suite uses actual FFmpeg, temporary files, local HTTP
servers and synthetic executable/provider responses. It covers discovery/skip
behavior, silence, timing bounds, invalid translations, separate credentials,
redirects, bounded retries, cancellation, owned-process cleanup and file safety.
These fixtures make no paid inference requests or model downloads.

Build/distribution checks verify package resources and manifest copies, unsafe
archive members, source/asset license metadata and notices, and the hash-pinned
public audio exception. Isolated wheel/source installs check imports, manifest
availability, help/version and subtitle publication outside the source checkout.
Installer tests cover upgrade/rollback and preservation of profiles, models and
unrelated files. Saved profile keys are endpoint-bound, and doctor/help make no
inference calls. See [contributing](../CONTRIBUTING.md) for exact commands.

GitHub Actions targets macOS/Linux and Python 3.11/3.13. Inspect the actual check
results for the revision you use; a configured workflow is not proof of a passing
hosted run. CI is protocol/packaging evidence, not live inference quality coverage.

## Quality and platform limits

On the public FLEURS sample, small Whisper misrecognized a phrase and the LLM
carried that error into translation. A specialist Japanese term also required the
sample glossary to pass validation. An offline CPU check produced another
semantically incorrect university phrase. These outcomes demonstrate functioning
inference and safeguards, not exact-word accuracy or a listening evaluation.

Korean real speech, long-form quality, hosted APIs, Windows, Linux/amd64 inference,
broader GPU behavior and aggregate peak memory remain unverified. Resource needs
depend on model/context/engine choices; no speed or minimum-RAM guarantee is made.
API STT requires timestamped verbose-JSON segments; text-only results and arbitrary
provider protocols are not supported. Failures do not silently switch providers
or publish partial translations.
