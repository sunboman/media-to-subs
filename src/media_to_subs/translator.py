"""Translate immutable timed cues in bounded sequential batches, without file I/O."""

from collections.abc import Iterator, Mapping

from ._llm_server import server_config
from .config import LLMConfig
from .errors import ConfigurationError
from .glossary import validate_glossary
from .llm import ChatClient, TranslationValidationError, build_messages, parse_translations
from .runtime import Cancellation
from .types import Progress, Segment, TimedTranscript

_VALIDATION_RETRIES = 1
_BATCH_CHARACTERS = 1200


def _batches(cues: tuple[Segment, ...], size: int) -> Iterator[tuple[int, tuple[Segment, ...]]]:
    start = 0
    while start < len(cues):
        end, characters = start, 0
        while end < len(cues) and end - start < size:
            added = len(cues[end].text) + len(cues[end].id) + 32
            if end > start and characters + added > _BATCH_CHARACTERS:
                break
            characters += added
            end += 1
        yield start, cues[start:end]
        start = end


def translate(transcript: TimedTranscript, config: LLMConfig,
              glossary: Mapping[str, str] | None, cancellation: Cancellation,
              progress: Progress, *, media_context: str | None = None) -> TimedTranscript:
    """Return validated Chinese cues; no partial result or implicit provider fallback.

    media_context is opt-in caller-supplied untrusted data. It is never inferred
    from files or provenance, logged, or added to output provenance.
    """
    cancellation.raise_if_cancelled()
    transcript.validate(allow_overlaps=transcript.allow_overlaps)
    if not transcript.segments or transcript.language.lower().replace("_", "-").split("-")[0] == "zh":
        progress("Translation skipped: silence or Chinese source")
        return transcript
    if media_context is not None and not isinstance(media_context, str):
        raise ConfigurationError("media_context must be explicit text or None")
    terms = validate_glossary(glossary)
    translated = []
    with server_config(config, cancellation, progress) as (active_config, check_alive):
        if config.mode == "agent":
            from .agents import AgentClient
            client = AgentClient(active_config, cancellation, progress)
        else:
            client = ChatClient(active_config, cancellation, progress)
        for start, cues in _batches(transcript.segments, config.batch_size):
            check_alive()
            end = start + len(cues)
            context = [s.text[:160] for s in transcript.segments[max(0, start - 1):start]
                       + transcript.segments[end:end + 1]]
            batch_glossary = {source: target for source, target in terms.items()
                              if any(source in cue.text for cue in cues)}
            for attempt in range(_VALIDATION_RETRIES + 1):
                check_alive()
                try:
                    response = client.chat(build_messages(cues, batch_glossary, context,
                                           correction=attempt > 0, media_context=media_context))
                    check_alive()
                    texts = parse_translations(response, cues, batch_glossary)
                    break
                except TranslationValidationError:
                    if attempt == _VALIDATION_RETRIES:
                        raise TranslationValidationError("LLM translation failed validation after one correction retry") from None
                    progress("Retrying invalid LLM translation (1/1)")
            translated.extend(Segment(cue.id, cue.start, cue.end, text) for cue, text in zip(cues, texts))
            progress(f"Translated {end}/{len(transcript.segments)} subtitle cues")
        check_alive()
    cancellation.raise_if_cancelled()
    model = config.agent_model if config.mode == "agent" else config.model or (config.model_path.name if config.managed_server else "local")
    # Configured model names are useful provenance, but never retain a key even
    # if a caller accidentally placed it in the model field too.
    for secret in (config.api_key, *config._redaction_secrets):
        if secret and model:
            model = model.replace(secret, "[redacted]")
    provenance = dict(transcript.provenance)
    provenance["translation"] = {"engine": config.agent if config.mode == "agent" else "openai-compatible", "mode": config.mode,
                                 "model": model, "source_language": transcript.language,
                                 "batch_size": config.batch_size, "managed_server": config.managed_server}
    if config.mode == "agent":
        provenance["translation"]["model_source"] = "configured" if config.agent_model else "cli-default"
    return TimedTranscript("zh", tuple(translated), provenance, allow_overlaps=transcript.allow_overlaps)
