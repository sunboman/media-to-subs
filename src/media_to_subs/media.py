"""Local media selection, audio-language guards and subtitle discovery."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import json
import math
import tempfile
import re
from pathlib import Path

from .errors import ProcessingError, ValidationError
from .config import STTConfig
from .runtime import Cancellation, run_process
from .subtitles import parse_srt
from .types import TimedTranscript


LANGUAGE_PRIORITY = {'zh-cn': 0, 'zh-tw': 1, 'ja': 2, 'ko': 3, 'en': 4}
_LANGUAGE_ALIASES = {
    'ja': 'ja', 'jp': 'ja', 'jpn': 'ja', 'japanese': 'ja', '日本語': 'ja',
    'ko': 'ko', 'kor': 'ko', 'korean': 'ko', '한국어': 'ko',
    'en': 'en', 'eng': 'en', 'english': 'en',
    'zh': 'zh-cn', 'chi': 'zh-cn', 'zho': 'zh-cn', 'chinese': 'zh-cn',
    'zh-cn': 'zh-cn', 'zh-hans': 'zh-cn', 'chs': 'zh-cn',
    'zh-tw': 'zh-tw', 'zh-hant': 'zh-tw', 'cht': 'zh-tw',
    'fr': 'fr', 'fre': 'fr', 'fra': 'fr', 'french': 'fr',
    'de': 'de', 'deu': 'de', 'ger': 'de', 'german': 'de',
    'es': 'es', 'spa': 'es', 'spanish': 'es',
    'it': 'it', 'ita': 'it', 'italian': 'it',
    'ru': 'ru', 'rus': 'ru', 'russian': 'ru',
    'pt': 'pt', 'por': 'pt', 'portuguese': 'pt',
}


@dataclass(frozen=True)
class AudioStreamInfo:
    index: int
    codec_name: str
    language: str | None
    raw_language: str | None
    title: str | None
    is_default: bool
    is_forced: bool


def normalize_stream_language(raw_language: str | None, title: str | None = None) -> str | None:
    """Never override a known unsupported tag using a misleading title."""
    if raw_language:
        cleaned = raw_language.strip().lower().replace('_', '-')
        if cleaned not in {'', 'und', 'unk', 'unknown', 'mis', 'mul', 'zxx'}:
            return _LANGUAGE_ALIASES.get(cleaned, cleaned)
    if title:
        for name, code in _LANGUAGE_ALIASES.items():
            if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', title.lower()):
                return code
    return None


def select_audio_stream(streams: list[AudioStreamInfo], *, preferred_language: str | None = None,
                        stream_index: int | None = None) -> AudioStreamInfo:
    """Choose a stable absolute stream index, preserving the wrong-dub guard.

    An explicit stream overrides ranking, never an explicitly known language
    mismatch. Auto prefers Japanese/Korean over a default dub, then unknown;
    known unsupported-only input requires the user to select suitable media.
    """
    if preferred_language == 'auto':
        preferred_language = None
    if preferred_language not in (None, 'ja', 'ko'):
        raise ValueError('Source language must be auto, ja or ko')
    ordered = sorted(streams, key=lambda s: (not s.is_default, s.index))
    if stream_index is not None:
        if isinstance(stream_index, bool) or not isinstance(stream_index, int) or stream_index < 0:
            raise ValueError('Audio stream must be a nonnegative absolute index')
        ordered = [s for s in ordered if s.index == stream_index]
    if not ordered:
        raise ValueError('No matching audio stream found in media')
    if preferred_language:
        matching = [s for s in ordered if s.language == preferred_language]
        unknown = [s for s in ordered if s.language is None]
        if matching or unknown:
            return (matching or unknown)[0]
        raise ValueError('No audio stream matching requested language; refusing a known wrong-language dub')
    supported = [s for s in ordered if s.language in ('ja', 'ko')]
    unknown = [s for s in ordered if s.language is None]
    if supported or unknown:
        return (supported or unknown)[0]
    raise ValueError('No Japanese, Korean or unlabelled audio stream; refusing an unsupported dub')


_TEXT_SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt'}
# Preserve recognized bitmap/other subtitle containers even though this adapter
# cannot decode them as authored text. Protection is broader than discovery.
_PROTECTED_SUBTITLE_EXTENSIONS = _TEXT_SUBTITLE_EXTENSIONS | {'.sub', '.idx', '.sup', '.pgs', '.mks'}
_TEXT_SUBTITLE_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'}
_PARTIAL = re.compile(r'(?<!\w)(?:forced|signs?|songs?|karaoke|lyrics?|commentary|partial|opening|ending|op|ed)(?!\w)|招牌|特效|强制|強制|片头|片尾|片頭|片尾', re.I)
_KANA = re.compile(r'[\u3040-\u30ff]')
_HANGUL = re.compile(r'[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]')
_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')


def language_from_name(name: str) -> str | None:
    """Classify subtitle suffixes/titles without relabelling explicit metadata."""
    raw = name.lower()
    if re.search(r'(?:^|[._ -])zh[._-]?(?:tw|hant)(?:[._ -]|$)', raw):
        return 'zh-tw'
    if re.search(r'(?:^|[._ -])zh[._-]?(?:cn|hans)(?:[._ -]|$)', raw):
        return 'zh-cn'
    tokens = set(re.sub(r'[^a-z0-9\u4e00-\u9fff]+', ' ', raw).split())
    for aliases, code in (({'zhcn','zhhans','chs','sc','简体','简中'}, 'zh-cn'),
                          ({'zhtw','zhhant','cht','tc','繁体','繁中'}, 'zh-tw'),
                          ({'ja','jpn','jp','japanese','日语','日文'}, 'ja'),
                          ({'ko','kor','korean','韩语','韩文'}, 'ko'),
                          ({'en','eng','english','英语','英文'}, 'en'),
                          ({'zh','chi','zho'}, 'zh-cn')):
        if tokens & aliases:
            return code
    # Reject known unsupported suffixes rather than guessing from Latin text.
    for token in tokens:
        if token in _LANGUAGE_ALIASES:
            return _LANGUAGE_ALIASES[token]
    return None


def detect_text_language(text: str) -> str | None:
    sample = re.sub(r'<[^>]*>|\{[^}]*\}', '', text[:20000])
    hangul, kana, cjk = len(_HANGUL.findall(sample)), len(_KANA.findall(sample)), len(_CJK.findall(sample))
    if hangul >= 3 and hangul >= kana and hangul >= cjk // 4:
        return 'ko'
    if kana >= 3 and kana >= cjk // 12:
        return 'ja'
    if cjk >= 8:
        return 'zh-cn'  # Chinese script is preserved as-is; no script conversion.
    if len(re.findall(r'[a-zA-Z]', sample)) >= 12:
        return 'en'
    return None


def _subtitle_language(transcript, hint: str | None) -> str | None:
    text = '\n'.join(segment.text for segment in transcript.segments)
    detected = detect_text_language(text)
    if hint is not None and hint not in LANGUAGE_PRIORITY:
        return None
    selected = hint or detected
    if selected in ('zh-cn', 'zh-tw'):
        # A source-language/bilingual payload cannot skip translation as Chinese.
        plain = re.sub(r'<[^>]*>|\{[^}]*\}', '', text)
        if not _CJK.search(plain) or _KANA.search(plain) or _HANGUL.search(plain):
            return None
    if selected == 'ja' and detected == 'ko' or selected == 'ko' and detected == 'ja':
        return None
    return selected


def _full_dialogue(transcript, duration: float, title: str = '', forced: bool = False) -> bool:
    """Conservative suitability heuristic, not an assertion of complete coverage."""
    if forced or _PARTIAL.search(title) or not transcript.segments:
        return False
    try:
        transcript.validate(allow_overlaps=True, duration=duration)
    except ValueError:
        return False
    cues = transcript.segments
    if duration >= 120:
        span = cues[-1].end - cues[0].start
        if cues[-1].end < duration * .35 or span < duration * .30 or len(cues) < max(3, int(duration / 120)):
            return False
    if duration >= 600:
        occupied = sum(cue.end - cue.start for cue in cues)
        if occupied < duration * .03 and len(cues) < duration / 30:
            return False
    return True


def sidecar_paths(media_path: Path) -> tuple[Path, ...]:
    """Enumerate protected same-basename subtitle candidates, without following links.

    This is also the output protection inventory: do not filter by language,
    validity, forced/partial status, file contents, or symlink target existence.
    Discovery applies suitability checks separately and never mutates these paths.
    """
    media = Path(media_path).absolute()
    prefix = media.stem.casefold() + '.'
    try:
        candidates = (path for path in media.parent.iterdir()
                      if path.name.casefold().startswith(prefix)
                      and path.suffix.lower() in _PROTECTED_SUBTITLE_EXTENSIONS)
        return tuple(sorted(candidates, key=lambda p: (p.name.casefold(), p.name)))
    except OSError:
        raise ProcessingError('Cannot enumerate local subtitle sidecars') from None


# Reject network protocols and playlist/concat demuxers before they can resolve
# secondary inputs. These are local media containers, not remote URL inputs.
_MEDIA_FORMATS = 'matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,avi,mpeg,mpegts,wav,mp3,flac,ogg,aac,aiff,asf'
_MAX_SUBTITLE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    audio_streams: tuple[AudioStreamInfo, ...]
    subtitle_streams: tuple[dict, ...]


def local_media_path(media_path: Path) -> Path:
    try:
        raw = str(media_path)
        if not raw or '\x00' in raw or re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', raw):
            raise ValueError
        path = Path(media_path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError
        return path
    except (OSError, ValueError, RuntimeError):
        raise ProcessingError('Input must be an existing nonempty local media file') from None


def inspect_media(media_path: Path, config: STTConfig, cancellation: Cancellation) -> MediaInfo:
    cancellation.raise_if_cancelled()
    path = local_media_path(media_path)
    result = run_process([
        config.ffprobe_binary, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-format_whitelist', _MEDIA_FORMATS, '-show_entries',
        'stream=index,codec_name,codec_type:stream_tags=language,title:stream_disposition=default,forced:format=duration',
        '-of', 'json', '-i', str(path),
    ], timeout=config.process_timeout, cancellation=cancellation, check=False)
    if result.returncode:
        raise ProcessingError('FFprobe could not inspect a supported local media file')
    try:
        data = json.loads(result.stdout)
        duration = float(data['format']['duration'])
        if not math.isfinite(duration) or duration <= 0 or not isinstance(data['streams'], list):
            raise ValueError
        audio, subtitles = [], []
        for stream in data['streams']:
            index = stream['index']
            if type(index) is not int or index < 0:
                raise ValueError
            if stream.get('codec_type') == 'subtitle':
                subtitles.append(stream)
            if stream.get('codec_type') != 'audio':
                continue
            tags, disposition = stream.get('tags', {}), stream.get('disposition', {})
            raw = str(tags.get('language', '')) or None
            title = str(tags.get('title', '')) or None
            audio.append(AudioStreamInfo(index, str(stream.get('codec_name', '')), normalize_stream_language(raw, title),
                                         raw, title, bool(disposition.get('default')), bool(disposition.get('forced'))))
        return MediaInfo(path, duration, tuple(audio), tuple(subtitles))
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ProcessingError('FFprobe returned invalid media duration or stream data') from None


def extract_audio(info: MediaInfo, stream: AudioStreamInfo, scratch: Path,
                  config: STTConfig, cancellation: Cancellation) -> Path:
    """Write only a new audio.wav inside the caller's private scratch directory."""
    if not scratch.is_dir() or scratch.is_symlink():
        raise ProcessingError('Audio extraction requires an owned scratch directory')
    output = scratch / 'audio.wav'
    if output.exists() or output.is_symlink():
        raise ProcessingError('Audio scratch output already exists')
    result = run_process([
        config.ffmpeg_binary, '-v', 'error', '-nostdin', '-n',
        '-protocol_whitelist', 'file,pipe', '-format_whitelist', _MEDIA_FORMATS,
        '-threads', str(config.cpu_threads), '-i', str(info.path), '-map', f'0:{stream.index}',
        '-vn', '-sn', '-dn', '-af', 'aresample=async=1:first_pts=0',
        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
        '-map_metadata', '-1', '-f', 'wav', str(output),
    ], timeout=config.process_timeout, cancellation=cancellation, check=False)
    if result.returncode or not output.is_file():
        raise ProcessingError('FFmpeg could not extract the selected audio stream')
    return output


def _converted_subtitle(path: Path, output: Path, config: STTConfig,
                        cancellation: Cancellation, stream_index: int | None = None) -> str | None:
    if stream_index is None and path.suffix.lower() == '.srt':
        try:
            if not path.is_file() or path.stat().st_size > _MAX_SUBTITLE_BYTES:
                return None
            return path.read_bytes().decode('utf-8-sig')
        except (OSError, UnicodeError):
            return None
    command = [config.ffmpeg_binary, '-v', 'error', '-nostdin', '-n',
               '-protocol_whitelist', 'file,pipe', '-format_whitelist',
               _MEDIA_FORMATS if stream_index is not None else 'ass,ssa,srt,webvtt', '-i', str(path)]
    if stream_index is not None:
        command += ['-map', f'0:{stream_index}']
    command += ['-c:s', 'srt', '-fs', str(_MAX_SUBTITLE_BYTES), str(output)]
    result = run_process(command, timeout=config.process_timeout, cancellation=cancellation, check=False)
    if result.returncode:
        return None
    try:
        if output.stat().st_size > _MAX_SUBTITLE_BYTES:
            return None
        return output.read_bytes().decode('utf-8-sig')
    except (OSError, UnicodeError):
        return None


def discover(media_path: Path, config: STTConfig, scratch: Path,
             cancellation: Cancellation, progress: Callable[[str], None]) -> TimedTranscript | None:
    """Reuse suitable local/embedded Chinese first, then Japanese/Korean/English.

    The same sidecar_paths inventory must also protect outputs in the pipeline.
    Every candidate is materialized only in private scratch; source files are
    never unlinked, rewritten or treated as stale inference caches.
    """
    cancellation.raise_if_cancelled()
    if config.force_transcribe:
        return None
    info = inspect_media(media_path, config, cancellation)
    if not scratch.is_dir() or scratch.is_symlink():
        raise ProcessingError('Subtitle discovery requires an owned scratch directory')
    candidates = []
    for path in sidecar_paths(media_path):
        if path.suffix.lower() not in _TEXT_SUBTITLE_EXTENSIONS:
            continue
        suffix = path.name[len(Path(media_path).stem):]
        hint = language_from_name(suffix)
        if hint is not None and hint not in LANGUAGE_PRIORITY or _PARTIAL.search(suffix):
            continue
        if not path.is_file():
            continue
        candidates.append((path, None, hint, suffix, False, 0))
    for stream in info.subtitle_streams:
        if stream.get('codec_name') not in _TEXT_SUBTITLE_CODECS:
            continue
        tags, dispositions = stream.get('tags', {}), stream.get('disposition', {})
        title = str(tags.get('title', ''))
        hint = normalize_stream_language(str(tags.get('language', ''))) or language_from_name(title)
        forced = bool(dispositions.get('forced'))
        if hint is not None and hint not in LANGUAGE_PRIORITY or forced or _PARTIAL.search(title):
            continue
        candidates.append((info.path, stream['index'], hint, title, forced, 1))
    suitable = []
    progress('Checking local and embedded subtitle tracks')
    try:
        with tempfile.TemporaryDirectory(prefix='discovery-', dir=scratch) as temp:
            for index, (path, stream_index, hint, title, forced, origin_rank) in enumerate(candidates):
                cancellation.raise_if_cancelled()
                payload = _converted_subtitle(path, Path(temp)/f'candidate-{index}.srt', config, cancellation, stream_index)
                if payload is None:
                    continue
                try:
                    parsed = parse_srt(payload, allow_overlaps=True)
                except ValidationError:
                    continue
                language = _subtitle_language(parsed, hint)
                if language not in LANGUAGE_PRIORITY or not _full_dialogue(parsed, info.duration, title, forced):
                    continue
                provenance = {'source_kind': 'sidecar' if stream_index is None else 'embedded',
                              'source_path': str(path), 'media_path': str(Path(media_path).absolute()),
                              'subtitle_stream': stream_index, 'engine': 'authored', 'model': None,
                              'media_duration': info.duration, 'source_language': language,
                              'selection': 'Chinese first; then ja, ko, en; sidecar before embedded within language'}
                result = TimedTranscript(language, parsed.segments, provenance, allow_overlaps=True)
                suitable.append(((LANGUAGE_PRIORITY[language], origin_rank, index), result))
    except OSError:
        raise ProcessingError('Could not create or read subtitle scratch files') from None
    if not suitable:
        return None
    result = min(suitable, key=lambda item: item[0])[1]
    progress(f"Selected {result.provenance['source_kind']} subtitles ({result.language})")
    return result
