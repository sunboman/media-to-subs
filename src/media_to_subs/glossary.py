"""Explicit JSON glossary loading; no directory discovery or settings imports."""

from collections.abc import Mapping
import json
from pathlib import Path

from .errors import ValidationError

_MAX_GLOSSARY_BYTES = 1_048_576


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate_glossary(glossary: Mapping[str, str] | None) -> dict[str, str]:
    """Copy an explicit source -> target mapping, rejecting ambiguous entries."""
    if glossary is None:
        return {}
    if not isinstance(glossary, Mapping):
        raise ValidationError("Glossary must be a JSON source-to-target object")
    result = {}
    for source, target in glossary.items():
        for value in (source, target):
            if (not isinstance(value, str) or not value.strip() or value != value.strip()
                    or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
                raise ValidationError("Glossary entries must be nonempty strings without surrounding whitespace or controls")
        result[source] = target
    return result


def read_glossary(path: Path) -> dict[str, str]:
    """Read only the supplied UTF-8 JSON file; never echo its path or contents."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_GLOSSARY_BYTES + 1)
        if len(raw) > _MAX_GLOSSARY_BYTES:
            raise ValueError("Glossary too large")
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
        if data is None:
            raise ValueError("Glossary is not an object")
        return validate_glossary(data)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ValidationError("Cannot read glossary as a valid JSON source-to-target object (maximum 1 MiB)") from None
