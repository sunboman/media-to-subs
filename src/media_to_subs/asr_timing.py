"""Whisper timing and PCM helpers.

The mapping is engine-specific. Validate original-region coverage after repair;
these boundaries are approximate speech regions, never forced word alignment."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import re
from typing import Callable
import wave

from .asr_quality import (TranscriptionQualityError, block_times_ms, ms_to_timestamp,
                          normalized_caption, require_quality, shift_block)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start_ms: int
    duration_ms: int


def split_wave(
    source: Path,
    output_dir: Path,
    chunk_seconds: float,
    overlap_seconds: float,
    *, check_cancelled: Callable[[], None] = lambda: None,
) -> list[AudioChunk]:
    """Split a PCM WAV into overlapping chunks without another decode pass."""
    if chunk_seconds <= 0:
        raise ValueError("ASR chunk length must be positive")
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValueError("ASR chunk overlap must be shorter than the chunk")

    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[AudioChunk] = []
    with wave.open(os.fspath(source), "rb") as reader:
        params = reader.getparams()
        frame_rate = reader.getframerate()
        total_frames = reader.getnframes()
        chunk_frames = max(1, round(chunk_seconds * frame_rate))
        overlap_frames = round(overlap_seconds * frame_rate)
        step_frames = chunk_frames - overlap_frames

        start_frame = 0
        index = 0
        while start_frame < total_frames:
            check_cancelled()
            frame_count = min(chunk_frames, total_frames - start_frame)
            reader.setpos(start_frame)
            frames = reader.readframes(frame_count)
            chunk_path = output_dir / f"chunk-{index:05d}.wav"
            with wave.open(os.fspath(chunk_path), "wb") as writer:
                writer.setparams(params)
                writer.writeframes(frames)
            chunks.append(
                AudioChunk(
                    path=chunk_path,
                    start_ms=round(start_frame * 1000 / frame_rate),
                    duration_ms=round(frame_count * 1000 / frame_rate),
                )
            )
            if start_frame + frame_count >= total_frames:
                break
            start_frame += step_frames
            index += 1
    return chunks


def merge_chunk_transcripts(
    chunk_results: list[tuple[AudioChunk, list[dict]]],
    overlap_seconds: float,
) -> list[dict]:
    """Merge local chunk timestamps using midpoint ownership in overlap regions."""
    merged: list[dict] = []
    owners: list[int] = []
    half_overlap_ms = overlap_seconds * 500

    for position, (chunk, blocks) in enumerate(chunk_results):
        lower_bound = chunk.start_ms + (half_overlap_ms if position else 0)
        upper_bound = math.inf
        if position + 1 < len(chunk_results):
            next_chunk = chunk_results[position + 1][0]
            upper_bound = next_chunk.start_ms + half_overlap_ms

        for block in blocks:
            start_ms, end_ms = block_times_ms(block)
            midpoint = chunk.start_ms + ((start_ms + end_ms) // 2)
            if midpoint < lower_bound or midpoint >= upper_bound:
                continue
            shifted = shift_block(block, chunk.start_ms, len(merged) + 1)
            if merged and owners[-1] == position - 1 and overlap_seconds > 0:
                previous = chunk_results[position - 1][0]
                shared_start = chunk.start_ms
                shared_end = min(previous.start_ms + previous.duration_ms,
                                 shared_start + overlap_seconds * 1000)
                left_start, left_end = block_times_ms(merged[-1])
                right_start, right_end = block_times_ms(shifted)
                left_text, right_text = normalized_caption(merged[-1]), normalized_caption(shifted)
                short, long = sorted((left_text, right_text), key=len)
                shared = (shared_start < shared_end
                          and left_start < shared_end and left_end > shared_start
                          and right_start < shared_end and right_end > shared_start)
                substantial_overlap = min(left_end, right_end) - max(left_start, right_start)
                # Only coalesce duplicate context from adjacent inputs. Keep
                # the complete independently decoded text and actual inferred
                # outer endpoints; never split words or estimate by text size.
                if (shared and len(short) >= 12 and len(short) * 2 >= len(long)
                        and (long.startswith(short) or long.endswith(short))
                        and substantial_overlap * 2 >= min(left_end-left_start, right_end-right_start)):
                    complete = merged[-1] if len(left_text) > len(right_text) else shifted
                    merged[-1] = {
                        **complete, "index": str(len(merged)), "text": list(complete["text"]),
                        "time": f"{ms_to_timestamp(min(left_start,right_start))} --> {ms_to_timestamp(max(left_end,right_end))}",
                    }
                    owners[-1] = position
                    log.info("Coalesced duplicate ASR context at adjacent chunk overlap")
                    continue
            merged.append(shifted)
            owners.append(position)

    if not merged:
        raise TranscriptionQualityError("Whisper produced no subtitle blocks")
    return merged


def validate_vad_audio(audio_path: Path, segments: list[tuple[int, int, int, int]]) -> int:
    with wave.open(os.fspath(audio_path), "rb") as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, 16000):
            raise TranscriptionQualityError("Whisper region recovery requires mono 16kHz PCM16")
        duration_ms = round(reader.getnframes() * 1000 / reader.getframerate())
    for i, (start, end, _, _) in enumerate(segments):
        tolerance = 40 if i == len(segments) - 1 else 0
        if not 0 <= start < duration_ms or end > duration_ms + tolerance:
            raise TranscriptionQualityError("Whisper speech region exceeds original audio")
    return duration_ms


def regions_covered(blocks: list[dict], segments: list[tuple[int, int, int, int]]) -> bool:
    return all(any(block_times_ms(b)[0] < end and block_times_ms(b)[1] > start
                   for b in blocks) for start, end, _, _ in segments)


def require_original_region_timing(audio_path: Path, blocks: list[dict], diagnostics: str) -> None:
    segments = validated_vad_segments(diagnostics)
    if not segments:
        raise TranscriptionQualityError("Whisper original speech regions unavailable or inconsistent")
    duration = validate_vad_audio(audio_path, segments)
    gaps = validated_vad_gaps(diagnostics)
    if segments[0][0] >= 2000:
        gaps.insert(0, (0, segments[0][0] - 200))
    if duration - segments[-1][1] >= 2000:
        gaps.append((segments[-1][1] + 300, duration))
    if overlaps_vad_gap(blocks, gaps):
        raise TranscriptionQualityError("Whisper original region timing still crosses long speech gaps")
    if not regions_covered(blocks, segments):
        raise TranscriptionQualityError("Whisper left an original speech region without captions")
    for left, right in zip(blocks, blocks[1:]):
        if block_times_ms(left)[1] > block_times_ms(right)[0]:
            raise TranscriptionQualityError("Whisper original region captions overlap")


def infer_original_regions(
    audio_path: Path, output_dir: Path, diagnostics: str,
    infer: Callable[[Path, Path], list[dict]],
    *, check_cancelled: Callable[[], None] = lambda: None,
) -> list[dict]:
    """Re-infer bounded original PCM regions using text-independent Silero times.

    The CLI's orig_start/end intervals come from VAD before text decoding.
    Group nearby speech with its intervening audio, preferring original VAD
    pauses before 20s over arbitrary overlapping windows. Split a single long
    VAD interval into disjoint 30s inputs only when no speech pause is known. Every region must return quality-passed content; no dropping a
    failed region/caption to make a chunk pass. These are approximate speech
    regions, never exact word alignments.
    """
    segments = validated_vad_segments(diagnostics)
    if not segments:
        raise TranscriptionQualityError("Whisper original speech regions unavailable or inconsistent")
    validate_vad_audio(audio_path, segments)
    output_dir.mkdir(parents=True, exist_ok=True)
    groups = []
    with wave.open(os.fspath(audio_path), "rb") as reader:
        params = reader.getparams()
        rate, frames = reader.getframerate(), reader.getnframes()
        for start, end, _, _ in segments:
            if groups and start - groups[-1][1] < 2000 and end - groups[-1][0] <= 19500:
                groups[-1][1] = end
            else:
                groups.append([start, end])
        results = []
        for index, (start, end) in enumerate(groups):
            check_cancelled()
            lo_ms, hi_ms = max(0, start - 200), end + 300
            if index:
                lo_ms = max(lo_ms, (groups[index-1][1] + start) // 2)
            if index + 1 < len(groups):
                hi_ms = min(hi_ms, (end + groups[index+1][0]) // 2)
            lo = round(lo_ms * rate / 1000)
            hi = min(frames, round(hi_ms * rate / 1000))
            if lo >= hi:
                raise TranscriptionQualityError("Whisper invalid original speech region")
            path = output_dir / f"region-{index:04d}.wav"
            reader.setpos(lo)
            with wave.open(os.fspath(path), "wb") as writer:
                writer.setparams(params)
                writer.writeframes(reader.readframes(hi - lo))
            windows = split_wave(path, output_dir / f"windows-{index:04d}", 30, 0)
            local_results = []
            for window_index, window in enumerate(windows):
                check_cancelled()
                log.info("Re-inferring original speech region %s-%sms window %s/%s",
                         round(lo * 1000 / rate), round(hi * 1000 / rate), window_index+1, len(windows))
                blocks = infer(window.path, output_dir / f"result-{index:04d}-{window_index:04d}")
                require_quality(blocks)
                bounded = []
                for block in blocks:
                    onset, finish = block_times_ms(block)
                    # v1.9.1 timestamp tokens use a 20ms grid. A final token
                    # can round past physical PCM by less than one step.
                    # Retract that endpoint only; larger overruns fail closed.
                    if window.duration_ms < finish < window.duration_ms + 20:
                        finish = window.duration_ms
                        block = {**block, "time": f"{ms_to_timestamp(onset)} --> {ms_to_timestamp(finish)}"}
                    if not 0 <= onset < finish <= window.duration_ms:
                        raise TranscriptionQualityError("Whisper region timing exceeds original audio window")
                    bounded.append(block)
                local_results.append((window, bounded))
            merged = merge_chunk_transcripts(local_results, 0)
            results.extend([shift_block(b, round(lo * 1000 / rate), len(results)+i+1)
                            for i,b in enumerate(merged)])
    require_quality(results)
    require_original_region_timing(audio_path, results, diagnostics)
    return results


def overlaps_vad_gap(blocks: list[dict], gaps: list[tuple[int, int]]) -> bool:
    return any(
        start < hi and end > lo
        for block in blocks
        for start, end in [block_times_ms(block)]
        for lo, hi in gaps
    )


def validated_vad_segments(diagnostics: str) -> list[tuple[int, int, int, int]] | None:
    """Validate the pinned CLI mapping before using its original coordinates."""
    counts = re.findall(
        r"^whisper_vad: detected (\d+) speech segments$", diagnostics, re.MULTILINE
    )
    rows = re.findall(
        r"^whisper_vad: vad_segment_info: orig_start: (\d+\.\d+), "
        r"orig_end: (\d+\.\d+), vad_start: (\d+\.\d+), vad_end: (\d+\.\d+)$",
        diagnostics, re.MULTILINE,
    )
    segments = [tuple(round(float(t) * 1000) for t in row) for row in rows]
    valid = len(counts) == 1 and int(counts[0]) == len(segments)
    for i, (start, end, processed_start, processed_end) in enumerate(segments):
        valid = valid and start < end and processed_start < processed_end
        duration_difference = (end - start) - (processed_end - processed_start)
        # Silero uses 512-sample (32ms at 16kHz) windows. Its final
        # orig_end can exceed the WAV, while copied samples are clamped.
        endpoint_tolerance = 40 if i == len(segments) - 1 else 10
        valid = valid and -10 <= duration_difference <= endpoint_tolerance
        if i:
            previous = segments[i - 1]
            valid = valid and start >= previous[1]
            # Pin the mapping contract: 100ms copied overlap + 100ms silence.
            valid = valid and abs(processed_start - previous[3] - 200) <= 10
    if not valid:
        return None
    return segments


def validated_vad_gaps(diagnostics: str) -> list[tuple[int, int]] | None:
    segments = validated_vad_segments(diagnostics)
    if segments is None:
        return None
    # Retain copied 100ms overlap plus 200ms context; only long gaps qualify.
    return [
        (left[1] + 300, right[0] - 200)
        for left, right in zip(segments, segments[1:])
        if right[0] - left[1] >= 2000
    ]


def repair_vad_timing(
    blocks: list[dict], diagnostics: str
) -> list[dict]:
    """Retract boundaries interpolated into long gaps removed by whisper.cpp.

    whisper.cpp 1.9.1 copies each VAD speech interval plus 100ms overlap,
    inserts 100ms silence, then linearly maps that silence back across the
    original gap. A timestamp token there can become seconds early/late.
    Use the *same inference's* original/processed VAD intervals, retaining
    the copied overlap and another 200ms context. This is speech-region
    boundary correction, not word alignment or amplitude-based trimming.

    Never split text, move a boundary within retained speech/context, or
    assign a caption wholly inside a removed gap to an arbitrary utterance.
    Unknown/inconsistent CLI diagnostics leave the original output intact.
    """
    if not blocks:
        return blocks

    def warn(message: str) -> None:
        log.warning(message)

    gaps = validated_vad_gaps(diagnostics)
    if gaps is None:
        warn(
            "Whisper VAD timing mapping unavailable or inconsistent; "
            "keeping approximate SRT timestamps"
        )
        return blocks

    repaired = []
    changed = 0
    spanning = 0
    for block in blocks:
        start, end = block_times_ms(block)
        new_start, new_end = start, end
        for gap_start, gap_end in gaps:
            if gap_start < start < gap_end:
                new_start = gap_end
            if gap_start < end < gap_end:
                new_end = gap_start
        if new_start >= new_end:
            warn(
                f"Whisper VAD timing ambiguous for caption {block['index']}; "
                "keeping approximate SRT timestamps"
            )
            repaired.append(block)
        elif (new_start, new_end) != (start, end):
            repaired.append({
                **block,
                "time": f"{ms_to_timestamp(new_start)} --> {ms_to_timestamp(new_end)}",
            })
            changed += 1
        else:
            repaired.append(block)
        kept_start, kept_end = block_times_ms(repaired[-1])
        if any(kept_start <= lo and kept_end >= hi for lo, hi in gaps):
            spanning += 1
    if changed:
        message = (
            f"Corrected {changed} Whisper caption boundary range(s) in removed "
            "VAD gaps; speech-region timing remains approximate"
        )
        log.info(message)
    if spanning:
        warn(
            f"{spanning} Whisper caption(s) still span long VAD gaps; "
            "word alignment is ambiguous, so text was not split"
        )
    return repaired
