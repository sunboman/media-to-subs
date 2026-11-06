"""Quality checks for timed transcription.

Internal SRT dictionaries are normalized by adapters into TimedTranscript values."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import math
import re
import unicodedata

from .errors import ProviderError


_TIMESTAMP_RE = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"(?P<separator>[,.])(?P<milliseconds>\d{3})$"
)


class TranscriptionQualityError(ProviderError):
    """Raised when an SRT contains a likely ASR hallucination."""


@dataclass(frozen=True)
class QualityIssue:
    kind: str
    timestamp_ms: int
    message: str

    def shifted(self, offset_ms: int) -> "QualityIssue":
        timestamp_ms = self.timestamp_ms + offset_ms
        return QualityIssue(
            kind=self.kind,
            timestamp_ms=timestamp_ms,
            message=_issue_message(self.kind, timestamp_ms),
        )


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    block_count: int
    unique_count: int
    issue: QualityIssue | None = None

    @property
    def status(self) -> str:
        return "passed" if self.passed else "failed"

    @property
    def message(self) -> str:
        if self.issue:
            return self.issue.message
        return (
            f"Passed transcription quality checks "
            f"({self.block_count} blocks, {self.unique_count} unique captions)"
        )


def timestamp_to_ms(value: str) -> int:
    """Convert an SRT timestamp to milliseconds."""
    match = _TIMESTAMP_RE.match(value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    return (
        int(match.group("hours")) * 3_600_000
        + int(match.group("minutes")) * 60_000
        + int(match.group("seconds")) * 1_000
        + int(match.group("milliseconds"))
    )


def ms_to_timestamp(value: int) -> str:
    """Convert milliseconds to an SRT timestamp."""
    value = max(0, int(value))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def block_times_ms(block: dict) -> tuple[int, int]:
    """Return the start and end of an SRT block in milliseconds."""
    try:
        start, end = block["time"].split("-->", 1)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Invalid SRT time range: {block!r}") from exc
    return timestamp_to_ms(start.strip()), timestamp_to_ms(end.strip())


def shift_block(block: dict, offset_ms: int, index: int | None = None) -> dict:
    """Copy a block while shifting its timestamps."""
    start_ms, end_ms = block_times_ms(block)
    return {
        "index": str(index if index is not None else block["index"]),
        "time": (
            f"{ms_to_timestamp(start_ms + offset_ms)} --> "
            f"{ms_to_timestamp(end_ms + offset_ms)}"
        ),
        "text": list(block["text"]),
    }


def normalized_caption(block: dict) -> str:
    """Normalize caption text for exact repetition analysis."""
    text = unicodedata.normalize("NFKC", " ".join(block.get("text", []))).lower()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def analyze_transcription(blocks: list[dict]) -> QualityReport:
    """Detect strong signals of a Whisper repetition loop."""
    if not blocks:
        issue = QualityIssue(
            kind="empty",
            timestamp_ms=0,
            message="Whisper produced no subtitle blocks",
        )
        return QualityReport(False, 0, 0, issue)

    captions = [normalized_caption(block) for block in blocks]
    unique_count = len({caption for caption in captions if caption})

    internal_repetition = _find_internal_repetition(blocks, captions)
    if internal_repetition:
        return QualityReport(
            False,
            len(blocks),
            unique_count,
            internal_repetition,
        )

    repeated = _find_frequent_caption(blocks, captions)
    if repeated:
        return QualityReport(False, len(blocks), unique_count, repeated)

    low_diversity = _find_low_diversity_window(blocks, captions)
    if low_diversity:
        return QualityReport(False, len(blocks), unique_count, low_diversity)

    return QualityReport(True, len(blocks), unique_count)


def require_quality(blocks: list[dict], offset_ms: int = 0) -> QualityReport:
    """Raise when transcript analysis identifies a likely hallucination."""
    report = analyze_transcription(blocks)
    if report.passed:
        return report
    issue = report.issue.shifted(offset_ms) if report.issue and offset_ms else report.issue
    raise TranscriptionQualityError(issue.message if issue else report.message)


def _find_internal_repetition(
    blocks: list[dict],
    captions: list[str],
    *,
    minimum_span: int = 24,
    minimum_repetitions: int = 8,
    maximum_unit_length: int = 12,
) -> QualityIssue | None:
    """Detect a short phrase repeated excessively within one caption."""
    for block, caption in zip(blocks, captions):
        maximum_length = min(maximum_unit_length, len(caption) // minimum_repetitions)
        for unit_length in range(1, maximum_length + 1):
            required_repetitions = max(
                minimum_repetitions,
                math.ceil(minimum_span / unit_length),
            )
            repeated_length = unit_length * required_repetitions
            for start in range(len(caption) - repeated_length + 1):
                unit = caption[start : start + unit_length]
                segment = caption[start : start + repeated_length]
                if segment == unit * required_repetitions:
                    start_ms, _ = block_times_ms(block)
                    return QualityIssue(
                        kind="internal_repetition",
                        timestamp_ms=start_ms,
                        message=_issue_message("internal_repetition", start_ms),
                    )
    return None


def _find_frequent_caption(
    blocks: list[dict],
    captions: list[str],
    *,
    minimum_occurrences: int = 12,
    interval_ms: int = 120_000,
) -> QualityIssue | None:
    occurrences: dict[str, deque[int]] = defaultdict(deque)
    for block, caption in zip(blocks, captions):
        if len(caption) < 3:
            continue
        start_ms, _ = block_times_ms(block)
        recent = occurrences[caption]
        recent.append(start_ms)
        while recent and start_ms - recent[0] > interval_ms:
            recent.popleft()
        if len(recent) >= minimum_occurrences:
            first_ms = recent[0]
            return QualityIssue(
                kind="repetition_loop",
                timestamp_ms=first_ms,
                message=_issue_message("repetition_loop", first_ms),
            )
    return None


def _find_low_diversity_window(
    blocks: list[dict],
    captions: list[str],
    *,
    window_size: int = 40,
) -> QualityIssue | None:
    if len(blocks) < window_size:
        return None

    for start in range(len(blocks) - window_size + 1):
        window = [caption for caption in captions[start : start + window_size] if caption]
        if len(window) < window_size * 0.9:
            continue
        start_ms, _ = block_times_ms(blocks[start])
        _, end_ms = block_times_ms(blocks[start + window_size - 1])
        duration_ms = end_ms - start_ms
        if duration_ms < 20_000 or duration_ms > 180_000:
            continue

        counts = Counter(window)
        unique_ratio = len(counts) / len(window)
        top_three_share = sum(
            count for _, count in counts.most_common(3)
        ) / len(window)
        repeated_substantial_text = any(
            len(text) >= 3 and count >= 8 for text, count in counts.items()
        )
        if (
            unique_ratio <= 0.20
            and top_three_share >= 0.75
            and repeated_substantial_text
        ):
            return QualityIssue(
                kind="low_diversity",
                timestamp_ms=start_ms,
                message=_issue_message("low_diversity", start_ms),
            )
    return None


def _issue_message(kind: str, timestamp_ms: int) -> str:
    timestamp = ms_to_timestamp(timestamp_ms).split(",", 1)[0]
    if kind == "empty":
        return "Whisper produced no subtitle blocks"
    if kind == "internal_repetition":
        return f"Whisper excessive repetition within a caption at {timestamp}"
    if kind == "low_diversity":
        return f"Whisper low-diversity loop detected at {timestamp}"
    return f"Whisper repetition loop detected at {timestamp}"
