"""Timestamp-capable Whisper multipart API, independent of local inference."""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

from ..asr_quality import TranscriptionQualityError, ms_to_timestamp, normalized_caption, require_quality
from ..media import normalize_stream_language
from ..config import STTConfig
from ..errors import ConfigurationError, ProviderError, ProcessTimeoutError
from ..runtime import Cancellation, run_process
from ..asr_timing import merge_chunk_transcripts, split_wave
from .common import bound_blocks, is_quiet_wave, require_language_text, wave_duration


def transcription_url(base_url: str) -> str:
    """Accept a base ending in /v1 or the explicit /audio/transcriptions URL."""
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                any(ord(c) < 33 for c in base_url)):
            raise ValueError
        _ = parsed.port
        path = parsed.path.rstrip('/')
        if not path.endswith('/audio/transcriptions'):
            path += '/audio/transcriptions'
        return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    except (TypeError, ValueError):
        raise ValueError('STT URL must be an HTTP(S) base or transcription URL without credentials, query or fragment') from None


def _finite_seconds(value: object) -> bool:
    """JSON integers can overflow float conversion; milliseconds must fit too."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and math.isfinite(value * 1000)
    except OverflowError:
        return False


def parse_verbose_json(payload: str, *, duration: float, language: str,
                       quiet: bool = False) -> tuple[str, list[dict]]:
    """Bounds come only from decoded PCM, never the provider's duration field.

    A <=20ms final timestamp-token rounding overrun can be retracted to the
    physical boundary. Negative starts, other overruns and overlaps fail closed.
    All returned block times are local to this upload; merging adds offsets once.
    """
    def invalid():
        return TranscriptionQualityError('STT API returned invalid, incomplete or out-of-range timed segments')
    if not _finite_seconds(duration) or duration <= 0 or language not in ('ja', 'ko', 'auto'):
        raise invalid()
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        raise invalid() from None
    if not isinstance(data, dict) or not isinstance(data.get('segments'), list):
        raise invalid()
    raw_language = data.get('language', data.get('detected_language'))
    if raw_language is not None and not isinstance(raw_language, str):
        raise invalid()
    detected = normalize_stream_language(raw_language)
    if detected is None and language != 'auto':
        detected = language
    if detected not in ('ja', 'ko') or (language != 'auto' and detected != language):
        raise TranscriptionQualityError('STT API language does not match supported Japanese/Korean source selection')
    if len(data['segments']) > 100_000:
        raise invalid()
    blocks, previous_end = [], 0.0
    for index, segment in enumerate(data['segments'], 1):
        if not isinstance(segment, dict):
            raise invalid()
        start, end, text = segment.get('start'), segment.get('end'), segment.get('text')
        if (any(not _finite_seconds(t) for t in (start, end))
                or not isinstance(text, str) or not text.strip() or len(text) > 100_000):
            raise invalid()
        if duration < end <= duration + 0.020 + 1e-9:
            end = duration
        if not 0 <= start < end <= duration or start < previous_end:
            raise invalid()
        previous_end = end
        start_ms, end_ms = round(start * 1000), round(end * 1000)
        if end_ms <= start_ms:
            raise invalid()
        blocks.append({'index': str(index), 'time': f'{ms_to_timestamp(start_ms)} --> {ms_to_timestamp(end_ms)}', 'text': text.strip().split('\n')})
    if 'text' in data:
        text = data['text']
        if not isinstance(text, str):
            raise invalid()
        claimed = normalized_caption({'text': [text]})
        actual = ''.join(normalized_caption(b) for b in blocks)
        if claimed != actual:
            raise invalid()
    if not blocks:
        if quiet:
            return detected, []
        raise TranscriptionQualityError('STT API returned no segments for non-quiet audio; speech may be missing')
    require_quality(blocks)
    return detected, blocks


_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


def request_chunk(audio_path: Path, config: STTConfig, language: str,
                  cancellation: Cancellation) -> str:
    """One request plus bounded transient retries, with no credentials in argv."""
    if not config.url or not config.model:
        raise ConfigurationError('API Whisper requires an explicit STT URL and model')
    url = transcription_url(config.url)
    data = {'url': url, 'api_key': config.api_key, 'model': config.model,
            'response_format': 'verbose_json', 'language': language,
            'audio_path': str(audio_path), 'max_upload_bytes': config.max_upload_bytes,
            'connect_timeout': config.connect_timeout, 'read_timeout': config.read_timeout}
    helper = Path(__file__).with_name('_http_worker.py')
    for attempt in range(config.max_retries + 1):
        cancellation.raise_if_cancelled()
        try:
            result = run_process([sys.executable, str(helper)],
                                 timeout=min(config.process_timeout, config.connect_timeout + config.read_timeout + 1),
                                 cancellation=cancellation, input_text=json.dumps(data))
            try:
                response = json.loads(result.stdout)
                if not isinstance(response, dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise ProviderError('STT HTTP helper returned an invalid response') from None
        except ProcessTimeoutError:
            response = {'error': 'timeout'}
        status = response.get('status')
        if status == 200 and isinstance(response.get('payload'), str):
            return response['payload']
        retryable = status in _TRANSIENT_STATUS or response.get('error') in ('network', 'timeout')
        if retryable and attempt < config.max_retries:
            cancellation.wait(min(2, .25 * 2 ** min(attempt, 3)))
            cancellation.raise_if_cancelled()
            continue
        if type(status) is int:
            raise ProviderError(f'STT API request failed with HTTP {status}; redirects are not followed')
        if response.get('error') == 'request':
            raise ProviderError('STT API request exceeded upload limits or has unsupported configuration')
        if response.get('error') == 'tls':
            raise ProviderError('STT API TLS verification failed')
        raise ProviderError('STT API request failed or timed out; retrying may repeat provider work')
    raise AssertionError('unreachable')


def transcribe_audio(audio_path: Path, config: STTConfig, scratch: Path,
                     cancellation: Cancellation, progress: Callable[[str], None]) -> tuple[str, list[dict]]:
    if config.source_language not in ('auto', 'ja', 'ko'):
        raise ConfigurationError('Whisper supports source_language auto, ja or ko')
    if not 4096 < config.max_upload_bytes <= 100_000_000:
        raise ConfigurationError('STT upload byte limit must be greater than 4096 and at most 100000000')
    # WAV header and generous multipart metadata allowance are counted before
    # splitting. Actual request bytes are checked again by the isolated helper.
    byte_frames = (config.max_upload_bytes - 4096 - 44) // 2
    seconds = min(config.chunk_duration, byte_frames / 16000)
    if seconds <= config.chunk_overlap or seconds < .02:
        raise ConfigurationError('STT upload byte limit is too small for configured chunk overlap')
    chunks = split_wave(audio_path, scratch / 'api-chunks', seconds, config.chunk_overlap,
                        check_cancelled=cancellation.raise_if_cancelled)
    results, detected = [], None
    for index, chunk in enumerate(chunks, 1):
        cancellation.raise_if_cancelled()
        progress(f'Transcribing API audio chunk {index}/{len(chunks)}')
        if is_quiet_wave(chunk.path, cancellation.raise_if_cancelled):
            results.append((chunk, []))
            continue
        payload = request_chunk(chunk.path, config, config.source_language, cancellation)
        language, blocks = parse_verbose_json(payload, duration=wave_duration(chunk.path), language=config.source_language)
        if detected is not None and language != detected:
            raise TranscriptionQualityError('STT API returned conflicting languages across chunks')
        detected = language
        require_language_text(blocks, detected)
        results.append((chunk, blocks))
    if detected is None:
        # Called directly on entirely digital silence; public entry point records
        # the same independent PCM evidence before selecting an engine.
        return config.source_language if config.source_language != 'auto' else 'und', []
    merged = merge_chunk_transcripts(results, config.chunk_overlap)
    require_quality(merged)
    return detected, bound_blocks(merged, wave_duration(audio_path))
