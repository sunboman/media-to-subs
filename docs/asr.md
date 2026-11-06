# Media preparation and Whisper

## Timing and quality safeguards

The transcription stage checks repetition, chunk boundaries, overlapping cues,
VAD mappings and original speech-region coverage. Recovery uses original PCM
speech regions and stays bounded. Unknown VAD diagnostics fail the adapter;
gap repair alone is not proof of valid timing. Failed captions are never dropped
to make a result pass. Synthetic fixtures cover these behaviors without bundling
private audio or episode excerpts.

## Protocol boundary

The API adapter targets the timestamp-capable Whisper multipart contract:
`POST /audio/transcriptions`, `model`, `response_format=verbose_json`,
`timestamp_granularities[]=segment`, and `language` only for explicit `ja`/`ko`.
A base URL or a full URL ending in `/audio/transcriptions` is accepted. Query,
fragment and URL credentials are rejected; use the separate API key field.
HTTP redirects are returned as errors, never followed. The upload filename is
always `audio.wav`, not the original media name. The default 24,000,000-byte cap
includes multipart overhead, below the documented 25 MB file limit.

References checked 2026-10-25: [OpenAI Whisper compatibility protocol](https://developers.openai.com/api/docs/guides/speech-to-text)
and [whisper.cpp v1.9.5 CLI](https://github.com/ggml-org/whisper.cpp/blob/v1.9.5/examples/cli/cli.cpp).
These document the selected shape, not arbitrary speech engines or all providers.

Timestamps must be finite numeric values, positive spans, ordered, nonoverlapping
and inside the decoded upload duration. A final endpoint up to 20 ms beyond the
PCM may be retracted to its actual end; larger overruns fail. The API `duration`
field is ignored. Missing segment arrays, plain text and language mismatches fail.
Empty output on non-quiet audio is failure, not evidence of no speech.

## Discovery and output protection interface

```python
from pathlib import Path
from media_to_subs.media import sidecar_paths

def sidecar_paths(media_path: Path) -> tuple[Path, ...]: ...
```

Discovery and the pipeline's output protection use this same function. It returns
absolute paths in stable filename order for text `.srt`, `.ass`, `.ssa`, `.vtt`
and recognized unsupported bitmap/container `.sub`, `.idx`, `.sup`, `.pgs`, `.mks`
(case-insensitive extensions) beside the media with its exact basename followed
by a dot, including optional language/other suffixes. It includes unselected,
malformed, partial, unsupported-language, hardlinked and symlinked candidates,
including dangling symlinks. It does not resolve links or validate contents.
Pass the entire tuple to the shared output protection/publication functions.
Discovery only sends the four text extensions to the parser/FFmpeg converter.
Selection may reject candidates; protection must not drop them from its inventory.
In particular, overwrite cannot replace authored bitmap sidecars merely because
those formats cannot be reused as text.

## Public adapters and provenance

```python
from media_to_subs.media import discover
from media_to_subs.asr import transcribe

def discover(media_path, config, scratch, cancellation, progress):
    """Return TimedTranscript or None for no suitable local subtitles."""

def transcribe(media_path, config, scratch, cancellation, progress):
    """Return strict TimedTranscript; raise on invalid/failed speech inference."""
```

Both match `docs/interfaces.md` exactly: Path, STTConfig, Path, Cancellation,
Callable[[str], None]. `scratch` must be an existing private directory. Each
entrypoint creates and cleans its own unique child. `transcribe` always takes the
audio path; callers run `discover` first. `force_transcribe` makes discovery return
None immediately. Only the selected path requires its engine/model.

Discovery provenance uses `source_kind="sidecar"` plus `source_path` for the
selected authored file. Embedded choices use `source_kind="embedded"`, the media
`source_path`, and absolute `subtitle_stream` index. This convention is the public
selection interface for pipeline target reuse. Both carry `source_language`,
`media_duration`, `engine="authored"` and selection explanation. Authored overlaps
are preserved using the foundation's explicit overlap option. Chinese script is
preserved; bilingual Chinese/Japanese/Korean payloads are not treated as Chinese
outputs. Explicit unsupported language tags are never relabelled from Latin text.
Within each language, sidecars precede embedded tracks; language ranking is
Chinese (Simplified-labelled, then Traditional-labelled), Japanese, Korean, English.

Forced/signs/songs/partial metadata is excluded. Long-media candidates with very
short/sparse coverage are rejected conservatively. This cannot prove that an
unlabelled track is complete dialogue; no online lookup or speech alignment is
performed during discovery. Unsupported bitmap subtitles require transcription.

Transcription provenance uses `source_kind="transcription"`, original
`source_path`, absolute `audio_stream`, measured `audio_duration`, engine/model,
chunk settings, `quality_checked`, `vad_enabled` and `vad_mapping_checked`.
`engine="pcm-silence"` means measured digital quiet (at most one PCM16 quantization
step), with inference and VAD skipped. Empty inference on other non-quiet audio is
an error unless a configured local VAD model produces validated zero speech regions.
API output never claims local VAD mapping/coverage checks, even when the remote
server may itself use VAD. API URL/key are excluded from provenance and errors.

FFmpeg allows only local file/pipe protocols and supported media demuxers, then
extracts mono 16 kHz PCM16. Timestamp-derived leading silence and gaps are retained
with `aresample=async=1:first_pts=0`; this is not amplitude trimming. Neither input
nor authored sidecars are used as an inference output/cache. Existing scratch
output is rejected, not unlinked. All processes run through shared runtime with a
finite configured deadline and cancellation; no persistent local Whisper server
is launched by the adapter.

## Local checks and recovery

Local mode requires explicit `model_path`, optional `vad_model`, and a configured
`whisper_binary`. CPU threads, chunk duration/overlap and process deadline are
explicit STT fields. The supported ASR languages are auto/ja/ko; authored English
subtitles can be reused, but this adapter does not promise English speech inference.
Auto detection samples opening and two later 30-second windows and requires two
matching supported votes (one for a <=30-second input). A known wrong-language dub
is rejected during stream selection, including explicit stream indices.

Configured VAD diagnostics must satisfy the original/processed mapping contract.
Gap repair retracts only boundaries inside removed long silence. If timing,
coverage or quality still fails, original contiguous speech regions are decoded
without VAD; every original region must retain captions. Quality failures may
subdivide into shorter chunks down to 15 seconds. Original parent coverage gates
survive subdivision. Failed speech, empty recovery, internal repetition at the
floor and ambiguous distinct overlaps fail rather than dropping captions.

Adjacent overlap ownership follows inferred cue midpoints. Substantial matching
partial duplicates can coalesce using actual inferred outer endpoints; short
repeats and distinct utterances remain distinct and final overlaps are rejected.
No proportional text timing, global shift or forced word-alignment claim is made.
Malformed UTF-8 SRT is rejected rather than silently replacing text.

API requests execute in an owned stdlib HTTP helper subprocess, with JSON passed
via stdin. This bounds cancellation during DNS, connection, upload and read. The
parent imposes a total deadline, the helper has separate connect/read limits, and
only HTTP 408/425/429/500/502/503/504 or transient network/timeouts are retried.
Authentication, redirects, TLS validation and response-shape failures are not
retried. A network timeout can occur after provider work starts; retry may repeat
that work. API SRT response mode is not exposed in this first adapter; tested
public requests use verbose_json segment timestamps only.

## Verified native boundary

The bounded adapter smoke used macOS arm64 whisper.cpp 1.9.5, the installed small
multilingual model, and Silero 6.2.0 on a locally generated 5.8679375-second Japanese
sample held outside the repository. Local auto language + VAD returned one ordered
cue at 0.070–5.690 seconds. The multipart adapter against an owned loopback native
server (configured with VAD) returned one cue at 0.070–5.760 seconds. Both passed
physical bounds and repetition checks; the server exited cleanly and input hash
was unchanged. The same local sample without VAD generated an excessive endpoint
and was correctly rejected. This is structural smoke evidence, not exact-word,
universal quality, paid-provider, Korean speech, Linux or GPU support certification.
No generated private speech sample, model or credential was copied into the package.
