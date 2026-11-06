"""Whisper local/API transcription with strict original-timeline output."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import tempfile

from ..config import STTConfig
from ..errors import ConfigurationError, ProcessingError
from ..media import extract_audio, inspect_media, select_audio_stream
from ..runtime import Cancellation
from ..types import TimedTranscript
from .common import is_quiet_wave, transcript_from_blocks, wave_duration


def transcribe(media_path: Path, config: STTConfig, scratch: Path,
               cancellation: Cancellation, progress: Callable[[str], None]) -> TimedTranscript:
    """Transcribe selected local media; discovery is the caller's preceding step.

    Local and API are independent explicit choices. A local failure cannot enter
    the API branch. Only the newly created child directory of scratch is removed.
    """
    cancellation.raise_if_cancelled()
    if config.source_language not in ('auto', 'ja', 'ko'):
        raise ConfigurationError('Whisper supports source_language auto, ja or ko')
    if not scratch.is_dir() or scratch.is_symlink():
        raise ProcessingError('Transcription requires an owned scratch directory')
    info = inspect_media(media_path, config, cancellation)
    try:
        selected = select_audio_stream(list(info.audio_streams), preferred_language=config.source_language,
                                       stream_index=config.audio_stream)
    except ValueError as exc:
        raise ProcessingError(str(exc)) from None
    progress(f'Selected audio stream {selected.index} ({selected.language or "unlabelled"})')
    try:
        with tempfile.TemporaryDirectory(prefix='transcription-', dir=scratch) as temp:
            work = Path(temp)
            audio = extract_audio(info, selected, work, config, cancellation)
            duration = wave_duration(audio)
            provenance = {'source_kind': 'transcription', 'source_path': str(Path(media_path).absolute()),
                          'audio_stream': selected.index, 'audio_language': selected.language,
                          'media_duration': info.duration, 'audio_duration': duration,
                          'engine': 'whisper-local' if config.mode == 'local' else 'whisper-api',
                          'model': str(config.model_path) if config.mode == 'local' else config.model,
                          'vad_enabled': False, 'vad_mapping_checked': False,
                          'quality_checked': False, 'chunk_duration': config.chunk_duration,
                          'chunk_overlap': config.chunk_overlap}
            if is_quiet_wave(audio, cancellation.raise_if_cancelled):
                language = config.source_language if config.source_language != 'auto' else selected.language or 'und'
                provenance.update(engine='pcm-silence', model=None, digital_silence=True)
                progress('Selected audio is digitally quiet; no inference needed')
                return TimedTranscript(language, (), provenance)
            if config.mode == 'local':
                from .local import transcribe_audio
                language, blocks = transcribe_audio(audio, config, work, cancellation, progress)
                provenance.update(vad_enabled=config.vad_model is not None,
                                  vad_mapping_checked=config.vad_model is not None,
                                  cpu_threads=config.cpu_threads,
                                  vad_model=str(config.vad_model) if config.vad_model else None)
            else:
                from .api import transcribe_audio
                language, blocks = transcribe_audio(audio, config, work, cancellation, progress)
                provenance.update(response_format='verbose_json', max_upload_bytes=config.max_upload_bytes)
            cancellation.raise_if_cancelled()
            provenance.update(source_language=language, quality_checked=bool(blocks), digital_silence=False)
            return transcript_from_blocks(blocks, language, duration, provenance)
    except OSError:
        raise ProcessingError('Could not create or read transcription scratch files') from None


__all__ = ['transcribe']
