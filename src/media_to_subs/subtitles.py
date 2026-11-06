"""Strict SRT parsing and serialization, preserving cue paragraphs and numeric dialogue."""

from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
import re

from .errors import ValidationError
from .types import Segment, TimedTranscript

_STAMP = r"([0-9]{2,}):([0-5][0-9]):([0-5][0-9]),([0-9]{3})"
_TIMING = re.compile(rf"{_STAMP} --> {_STAMP}")
_NUMBER = re.compile(r"[0-9]+")
_STAMP_PREFIX = re.compile(r"\s*[0-9]+:[0-9]{2}:[0-9]{2}")


def _seconds(parts: tuple[str, ...]) -> float:
    try:
        hours, minutes, seconds, millis = map(int, parts)
        return (hours * 3600000 + minutes * 60000 + seconds * 1000 + millis) / 1000
    except (OverflowError, ValueError):
        raise ValidationError("SRT timestamp is out of range") from None


def parse_srt(text: str, *, language: str = "und",
              provenance: Mapping[str, object] | None = None,
              allow_overlaps: bool = False) -> TimedTranscript:
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("SRT payload must be nonempty text")
    normalized = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    headers: list[tuple[str, float, float]] = []
    contents: list[list[str]] = []
    numbers: set[int] = set()
    # FFmpeg preserves ASS paragraph breaks. A blank line alone therefore does
    # not end a cue: a following number + timing header must establish it.
    parts = re.split(r"(\n(?:[ \t]*\n)+)", normalized)
    for position in range(0, len(parts), 2):
        block = parts[position]
        lines = block.split("\n")
        has_timing = len(lines) >= 2 and ("-->" in lines[1] or _STAMP_PREFIX.match(lines[1]))
        if headers and not has_timing:
            if any(_STAMP_PREFIX.match(line) or _TIMING.fullmatch(line) for line in lines):
                raise ValidationError("SRT timing line is missing a valid cue header")
            contents[-1].append(parts[position - 1] + block)
            continue
        if len(lines) < 3 or not _NUMBER.fullmatch(lines[0]):
            raise ValidationError("SRT cue requires a positive number, timing line and text")
        if not "\n".join(lines[2:]).strip():
            raise ValidationError("SRT cue content must begin before any paragraph break")
        try:
            number = int(lines[0])
        except ValueError:
            raise ValidationError("SRT cue number is out of range") from None
        if number <= 0 or number in numbers:
            raise ValidationError("SRT cue numbers must be positive and unique")
        numbers.add(number)
        timing = _TIMING.fullmatch(lines[1])
        if timing is None:
            raise ValidationError("Unsupported or invalid SRT timing line")
        if any(_STAMP_PREFIX.match(line) or _TIMING.fullmatch(line) for line in lines[2:]):
            raise ValidationError("SRT cues must be separated by a blank line")
        headers.append((lines[0], _seconds(timing.groups()[:4]), _seconds(timing.groups()[4:])))
        contents.append(["\n".join(lines[2:])])
    segments = tuple(Segment(*header, "".join(body)) for header, body in zip(headers, contents))
    return TimedTranscript(language, tuple(segments), provenance if provenance is not None else {},
                           allow_overlaps=allow_overlaps)


def _milliseconds(seconds: float) -> int:
    return int((Decimal(str(seconds)) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def _timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def serialize_srt(transcript: TimedTranscript, *, allow_overlaps: bool = False) -> str:
    transcript.validate(allow_overlaps=allow_overlaps)
    if not transcript.segments:
        raise ValidationError("Cannot serialize an empty transcript to SRT")
    numeric_ids = all(_NUMBER.fullmatch(segment.id) for segment in transcript.segments)
    if numeric_ids:
        try:
            numbers = [int(segment.id) for segment in transcript.segments]
        except ValueError:
            raise ValidationError("SRT cue number is out of range") from None
        if min(numbers) <= 0 or len(set(numbers)) != len(numbers):
            raise ValidationError("SRT cue numbers must be positive and numerically unique")
    blocks = []
    for index, segment in enumerate(transcript.segments, 1):
        lines = segment.text.split("\n")
        if not lines[0].strip() or not lines[-1].strip():
            raise ValidationError("SRT cue text cannot begin or end with blank lines")
        if any(_STAMP_PREFIX.match(line) or _TIMING.fullmatch(line) for line in lines):
            raise ValidationError("SRT cue text contains an ambiguous timing header")
        start, end = _milliseconds(segment.start), _milliseconds(segment.end)
        if start >= end:
            raise ValidationError("SRT cue collapses at millisecond precision")
        cue_number = segment.id if numeric_ids else str(index)
        blocks.append(f"{cue_number}\n{_timestamp(start)} --> {_timestamp(end)}\n{segment.text}")
    return "\n\n".join(blocks) + "\n"
