"""Explicit whisper.cpp adapter with bounded original-audio recovery."""
from __future__ import annotations

from collections.abc import Callable
import math
from pathlib import Path
import re
import shutil
import wave

from ..asr_quality import TranscriptionQualityError, require_quality, ms_to_timestamp
from ..asr_timing import (AudioChunk, merge_chunk_transcripts, repair_vad_timing,
                          require_original_region_timing, split_wave, validated_vad_segments,
                          validate_vad_audio, infer_original_regions)
from ..media import normalize_stream_language
from .common import bound_blocks, is_quiet_wave, require_language_text, wave_duration
from ..config import STTConfig
from ..errors import ConfigurationError, ProcessingError, ValidationError
from ..runtime import Cancellation, run_process
from ..subtitles import parse_srt

MIN_RECOVERY_SECONDS = 15


class OriginalRegionQualityError(TranscriptionQualityError):
    """Retain the parent's original speech coverage contract during subdivision."""
    def __init__(self, message: str, diagnostics: str):
        super().__init__(message)
        self.diagnostics = diagnostics


def parse_detect_language_output(output: str) -> str:
    # Do not mistake requested `language=auto` or timing-table tokens for results.
    candidates = re.findall(r'(?:auto-detected|detected)\s+language\s*[:=]\s*[\'\"]?([a-zA-Z-]+)', output, re.I)
    if not candidates:
        candidates = re.findall(r'"(?:detected_language|language)"\s*:\s*"([^\"]+)"', output)
    for candidate in candidates:
        language = normalize_stream_language(candidate)
        if language == 'auto':
            continue
        if language in ('ja', 'ko'):
            return language
        raise TranscriptionQualityError('Automatic language detection returned an unsupported language; select ja or ko explicitly')
    raise TranscriptionQualityError('Automatic language detection did not return a supported language; select ja or ko explicitly')


def language_samples(audio_path: Path, output_dir: Path) -> list[Path]:
    """Opening plus two later 30-second PCM windows; no extra decode or seeking."""
    with wave.open(str(audio_path), 'rb') as reader:
        rate, frames = reader.getframerate(), reader.getnframes()
        count = min(rate * 30, frames)
        if not count:
            raise TranscriptionQualityError('Automatic detection found empty audio')
        if frames <= count:
            return [audio_path]
        output_dir.mkdir(parents=True, exist_ok=True)
        last = frames - count
        samples = []
        for index, start in enumerate(dict.fromkeys((0, last // 3, 2 * last // 3))):
            reader.setpos(start)
            path = output_dir / f'language-{index}.wav'
            with wave.open(str(path), 'wb') as writer:
                writer.setparams(reader.getparams())
                writer.writeframes(reader.readframes(count))
            samples.append(path)
        return samples


def recover_chunk(chunk: AudioChunk, output_dir: Path,
                  infer: Callable[[Path, Path], list[dict]], *,
                  recovery_seconds: float = 60, overlap_seconds: float = 2,
                  check_cancelled: Callable[[], None] = lambda: None) -> list[dict]:
    """Recover only quality failures, never process errors or cancellation.

    At the 15-second floor a bad caption is an error. Nothing is discarded to
    force acceptance. Parent VAD regions and all physical timing gates survive
    recursive subdivision. Each recursion returns times local to its own input.
    """
    check_cancelled()
    output_dir.mkdir(parents=True, exist_ok=True)
    failure = None
    try:
        blocks = infer(chunk.path, output_dir / 'primary')
        if not blocks:
            return []  # infer must have independent quiet/VAD evidence.
        require_quality(blocks)
        return bound_blocks(blocks, wave_duration(chunk.path))
    except TranscriptionQualityError as exc:
        failure = exc
    seconds = min(recovery_seconds, max(MIN_RECOVERY_SECONDS, math.ceil(chunk.duration_ms / 2000)))
    if seconds * 1000 >= chunk.duration_ms:
        raise failure
    overlap = min(overlap_seconds, seconds - 1)
    children = split_wave(chunk.path, output_dir / 'chunks', seconds, overlap,
                          check_cancelled=check_cancelled)
    results = []
    for index, child in enumerate(children):
        blocks = recover_chunk(child, output_dir / f'recovery-{index}', infer,
                               recovery_seconds=max(MIN_RECOVERY_SECONDS, seconds // 2),
                               overlap_seconds=min(overlap, max(MIN_RECOVERY_SECONDS, seconds // 2) - 1),
                               check_cancelled=check_cancelled)
        results.append((child, blocks))
    if not any(blocks for _, blocks in results):
        raise failure
    merged = merge_chunk_transcripts(results, overlap)
    if isinstance(failure, OriginalRegionQualityError):
        merged = repair_vad_timing(merged, failure.diagnostics)
        require_original_region_timing(chunk.path, merged, failure.diagnostics)
    require_quality(merged)
    return bound_blocks(merged, wave_duration(chunk.path))


def validate_local_config(config: STTConfig) -> None:
    if config.model_path is None or not config.model_path.is_file():
        raise ConfigurationError('Local Whisper requires an explicit readable model_path')
    if not shutil.which(config.whisper_binary):
        raise ConfigurationError('Configured whisper.cpp binary is unavailable')
    if config.vad_model is not None and not config.vad_model.is_file():
        raise ConfigurationError('Whisper VAD requires the configured local model file')


def _run_raw(audio_path: Path, output_prefix: Path, config: STTConfig,
             language: str, cancellation: Cancellation, *, vad: bool) -> tuple[list[dict], str]:
    cancellation.raise_if_cancelled()
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    output_path = output_prefix.with_suffix('.srt')
    if output_path.exists() or output_path.is_symlink():
        raise ProcessingError('Whisper scratch output already exists; refusing stale reuse')
    command = [config.whisper_binary, '-m', str(config.model_path), '-f', str(audio_path),
               '-l', language, '-t', str(config.cpu_threads), '-osrt', '-of', str(output_prefix)]
    if vad:
        command += ['--vad', '--vad-model', str(config.vad_model), '--vad-samples-overlap', '0.1']
    result = run_process(command, timeout=config.process_timeout, cancellation=cancellation)
    cancellation.raise_if_cancelled()
    try:
        if output_path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError
        raw = output_path.read_bytes().decode('utf-8-sig')
        if not raw.strip():
            blocks = []
        else:
            parsed = parse_srt(raw, language=language, allow_overlaps=True)
            blocks = [{'index': segment.id,
                       'time': f'{ms_to_timestamp(round(segment.start*1000))} --> {ms_to_timestamp(round(segment.end*1000))}',
                       'text': segment.text.split('\n')} for segment in parsed.segments]
    except (OSError, ValueError, UnicodeError, ValidationError):
        raise TranscriptionQualityError('whisper.cpp did not produce a valid fresh UTF-8 SRT') from None
    return blocks, result.stderr + '\n' + result.stdout


def run_whisper(audio_path: Path, output_prefix: Path, config: STTConfig,
                language: str, cancellation: Cancellation) -> list[dict]:
    """Infer one PCM input, repair/reinfer original VAD regions once if needed."""
    if is_quiet_wave(audio_path, cancellation.raise_if_cancelled):
        return []
    duration = wave_duration(audio_path)
    vad = config.vad_model is not None
    blocks, diagnostics = _run_raw(audio_path, output_prefix, config, language, cancellation, vad=vad)
    if not vad:
        require_quality(blocks)  # Empty non-quiet inference is a failure.
        require_language_text(blocks, language)
        return bound_blocks(blocks, duration)
    segments = validated_vad_segments(diagnostics)
    if segments is None:
        raise TranscriptionQualityError('Whisper VAD mapping is unavailable or inconsistent')
    if not segments:
        if blocks:
            raise TranscriptionQualityError('Whisper returned text without detected speech regions')
        return []
    validate_vad_audio(audio_path, segments)
    try:
        repaired = repair_vad_timing(blocks, diagnostics)
        repaired = bound_blocks(repaired, duration)
        require_original_region_timing(audio_path, repaired, diagnostics)
        require_quality(repaired)
        require_language_text(repaired, language)
        return repaired
    except TranscriptionQualityError:
        # Use the same binary/model/language on contiguous original PCM. No VAD
        # concatenation or invented word timing in the reinference path.
        def infer_region(path: Path, prefix: Path) -> list[dict]:
            result, _ = _run_raw(path, prefix, config, language, cancellation, vad=False)
            require_language_text(result, language)
            return result
        try:
            return infer_original_regions(audio_path, output_prefix.with_name(output_prefix.name + '-regions'),
                                          diagnostics, infer_region,
                                          check_cancelled=cancellation.raise_if_cancelled)
        except TranscriptionQualityError as exc:
            raise OriginalRegionQualityError(str(exc), diagnostics) from None


def detect_language(audio_path: Path, config: STTConfig, scratch: Path,
                    cancellation: Cancellation) -> str:
    samples = language_samples(audio_path, scratch / 'language-samples')
    votes = []
    for sample in samples:
        cancellation.raise_if_cancelled()
        if is_quiet_wave(sample, cancellation.raise_if_cancelled):
            continue
        result = run_process([config.whisper_binary, '-m', str(config.model_path), '-f', str(sample),
                              '-dl', '-t', str(config.cpu_threads)],
                             timeout=config.process_timeout, cancellation=cancellation)
        try:
            votes.append(parse_detect_language_output(result.stderr + '\n' + result.stdout))
        except TranscriptionQualityError:
            continue
    for language in ('ja', 'ko'):
        if votes.count(language) >= 2 or len(samples) == 1 and votes == [language]:
            return language
    raise TranscriptionQualityError('Automatic language samples do not agree on Japanese or Korean; select ja or ko explicitly')


def transcribe_audio(audio_path: Path, config: STTConfig, scratch: Path,
                     cancellation: Cancellation, progress: Callable[[str], None]) -> tuple[str, list[dict]]:
    validate_local_config(config)
    if config.source_language not in ('auto', 'ja', 'ko'):
        raise ConfigurationError('Local Whisper supports source_language auto, ja or ko')
    language = config.source_language
    if language == 'auto':
        progress('Detecting Japanese or Korean speech')
        language = detect_language(audio_path, config, scratch, cancellation)
    chunks = split_wave(audio_path, scratch / 'local-chunks', config.chunk_duration, config.chunk_overlap,
                        check_cancelled=cancellation.raise_if_cancelled)
    results = []
    for index, chunk in enumerate(chunks, 1):
        cancellation.raise_if_cancelled()
        progress(f'Transcribing local audio chunk {index}/{len(chunks)}')
        blocks = recover_chunk(chunk, scratch / f'local-{index}',
                               lambda path, prefix: run_whisper(path, prefix, config, language, cancellation),
                               check_cancelled=cancellation.raise_if_cancelled)
        results.append((chunk, blocks))
    if not any(blocks for _, blocks in results):
        return language, []
    merged = merge_chunk_transcripts(results, config.chunk_overlap)
    require_quality(merged)
    require_language_text(merged, language)
    return language, bound_blocks(merged, wave_duration(audio_path))
