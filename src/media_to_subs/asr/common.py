"""Shared physical-audio bounds for local and API Whisper."""
from __future__ import annotations

from array import array
from collections.abc import Callable, Mapping
import math
from pathlib import Path
import re
import sys
import wave

from ..asr_quality import TranscriptionQualityError, block_times_ms, ms_to_timestamp
from ..errors import ProcessingError, ValidationError
from ..types import Segment, TimedTranscript


def wave_duration(path: Path) -> float:
    try:
        with wave.open(str(path), 'rb') as reader:
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ProcessingError('Expected mono 16kHz PCM16 audio')
            duration = reader.getnframes() / reader.getframerate()
            if not math.isfinite(duration) or duration <= 0:
                raise ProcessingError('Decoded audio is empty')
            return duration
    except (OSError, wave.Error, EOFError):
        raise ProcessingError('Cannot read decoded PCM audio') from None


def is_quiet_wave(path: Path, check_cancelled: Callable[[], None] = lambda: None) -> bool:
    """Digital silence only (at most one PCM16 quantization step), not VAD.

    This does not trim or move timings. Non-quiet audio with no inference result
    fails unless the configured local VAD provides validated no-speech evidence.
    """
    wave_duration(path)
    with wave.open(str(path), 'rb') as reader:
        while True:
            check_cancelled()
            raw = reader.readframes(16000)
            if not raw:
                return True
            samples = array('h', raw)
            if sys.byteorder != 'little':
                samples.byteswap()
            if any(abs(s) > 1 for s in samples):
                return False


def bound_blocks(blocks: list[dict], duration: float, *, allow_overlaps: bool = False) -> list[dict]:
    bounded = []
    previous_start, previous_end = 0, 0
    physical_ms = duration * 1000
    for block in blocks:
        start, end = block_times_ms(block)
        if physical_ms < end <= physical_ms + 20 + 1e-6:
            end = round(physical_ms)
        if not 0 <= start < end <= round(physical_ms) or start < previous_start or (not allow_overlaps and start < previous_end):
            raise TranscriptionQualityError('Whisper timestamps exceed physical audio or overlap')
        previous_start, previous_end = start, end
        bounded.append({**block, 'time': f'{ms_to_timestamp(start)} --> {ms_to_timestamp(end)}', 'text': list(block['text'])})
    return bounded


def transcript_from_blocks(blocks: list[dict], language: str, duration: float,
                           provenance: Mapping[str, object]) -> TimedTranscript:
    try:
        segments = []
        for index, block in enumerate(bound_blocks(blocks, duration), 1):
            start, end = block_times_ms(block)
            # Millisecond block representation can round the measured endpoint
            # up by <0.5ms. Keep the public cue inside actual PCM seconds.
            segments.append(Segment(str(index), start / 1000, min(end / 1000, duration), '\n'.join(block['text'])))
        result = TimedTranscript(language, tuple(segments), provenance)
        result.validate(duration=duration)
        return result
    except (ValueError, ValidationError):
        raise TranscriptionQualityError('Whisper produced invalid final timing') from None


def require_language_text(blocks: list[dict], language: str) -> None:
    """Reject clear language inversions without pretending to identify every word."""
    text = '\n'.join('\n'.join(block['text']) for block in blocks)
    hangul = len(re.findall(r'[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]', text))
    kana = len(re.findall(r'[\u3040-\u30ff]', text))
    if (language == 'ja' and hangul >= 5 and hangul > kana * 2) or (language == 'ko' and kana >= 5 and kana > hangul * 2):
        raise TranscriptionQualityError('Whisper transcript conflicts with selected source language')
