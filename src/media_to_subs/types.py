"""Immutable timed text; seconds refer to the original media timeline."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import math
from types import MappingProxyType

from .errors import ValidationError

Progress = Callable[[str], None]


def _finite_time(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValidationError("Provenance keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValidationError("Provenance must contain finite JSON-compatible values")


@dataclass(frozen=True)
class Segment:
    id: str
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or any(c.isspace() or ord(c) < 32 for c in self.id):
            raise ValidationError("Segment ID must be a nonempty whitespace-free string")
        if not _finite_time(self.start) or not _finite_time(self.end) or self.end <= self.start:
            raise ValidationError("Segment times must be finite, nonnegative, with end after start")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValidationError("Segment text must be nonempty")
        if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in self.text):
            raise ValidationError("Segment text contains unsupported control characters")


@dataclass(frozen=True)
class TimedTranscript:
    language: str
    segments: tuple[Segment, ...]
    provenance: Mapping[str, object]
    allow_overlaps: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not self.language or self.language.strip() != self.language:
            raise ValidationError("Transcript language must be a nonempty language tag")
        if not isinstance(self.provenance, Mapping):
            raise ValidationError("Transcript provenance must be a mapping")
        if type(self.allow_overlaps) is not bool:
            raise ValidationError("allow_overlaps must be a boolean")
        try:
            object.__setattr__(self, "segments", tuple(self.segments))
        except TypeError:
            raise ValidationError("Transcript segments must be an iterable of Segment") from None
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        self.validate(allow_overlaps=self.allow_overlaps)

    def validate(self, *, allow_overlaps: bool = False, duration: float | None = None) -> None:
        if type(allow_overlaps) is not bool:
            raise ValidationError("allow_overlaps must be a boolean")
        if duration is not None and not _finite_time(duration):
            raise ValidationError("Media duration must be finite and nonnegative")
        seen: set[str] = set()
        previous: Segment | None = None
        for segment in self.segments:
            if not isinstance(segment, Segment):
                raise ValidationError("Transcript segments must be Segment instances")
            if segment.id in seen:
                raise ValidationError("Transcript contains duplicate segment IDs")
            seen.add(segment.id)
            if previous is not None:
                if segment.start < previous.start:
                    raise ValidationError("Transcript starts must be ordered")
                if not allow_overlaps and segment.start < previous.end:
                    raise ValidationError("Transcript contains overlapping segments")
            if duration is not None and segment.end > duration:
                raise ValidationError("Segment extends beyond media duration")
            previous = segment
