"""Sequential media-to-subtitle orchestration with explicit per-run ownership."""

from dataclasses import dataclass
from pathlib import Path
import shutil

from .config import RunConfig
from .errors import ProcessingError, ValidationError
from .runtime import Cancellation
from .subtitles import parse_srt, serialize_srt
from .types import Progress, TimedTranscript


@dataclass(frozen=True)
class RunResult:
    output: Path | None
    source_output: Path | None = None
    silent: bool = False


def run(media_path: Path, config: RunConfig, cancellation: Cancellation,
        progress: Progress) -> RunResult:
    """Discover, transcribe, translate and publish one local media file.

    No handlers or global configuration are installed. Inference adapters finish
    and release their owned resources before the next stage begins. Publication
    is atomic per file; a later source-export failure does not undo a completed
    target. All known authored sidecars are protected, including rejected tracks.
    """
    from .asr import transcribe
    from .files import atomic_publish, scratch_directory, validate_output_path
    from .glossary import read_glossary
    from .media import discover, sidecar_paths
    from .translator import translate

    cancellation.raise_if_cancelled()
    media_path = Path(media_path).absolute()
    try:
        if not media_path.is_file():
            raise ProcessingError("Input media must be an existing regular file")
        output = config.output or media_path.with_suffix(".zh.srt")
        sidecars = tuple(sidecar_paths(media_path))
        protected = (media_path, *sidecars)
        # Explicit non-sidecar inputs also belong to the user, never to this run.
        protected += tuple(path for path in (config.glossary, config.stt.model_path,
            config.stt.vad_model, config.llm.model_path) if path is not None)
        binaries = [config.stt.whisper_binary, config.stt.ffmpeg_binary,
                    config.stt.ffprobe_binary, config.llm.server_binary]
        if config.llm.mode == "agent":
            binaries.append(config.llm.agent_binary or config.llm.agent)
        for binary in binaries:
            resolved_binary = shutil.which(binary)
            if resolved_binary is not None:
                protected += (Path(resolved_binary),)

        with scratch_directory() as scratch:
            source = None
            if not config.stt.force_transcribe:
                progress("Looking for suitable existing subtitles")
                source = discover(media_path, config.stt, scratch, cancellation, progress)
            cancellation.raise_if_cancelled()

            # Output and export preflight happens before either inference stage.
            # The selected Chinese sidecar may already be the exact requested
            # destination. Its authored bytes are preserved without publication.
            already_present = _selected_target(output, source)
            outputs = (config.save_source,) if config.save_source is not None else ()
            if already_present:
                output = Path(output).absolute()
                if config.save_source is not None:
                    validate_output_path(output, input_path=media_path,
                                         sidecars=outputs, overwrite=True)
            else:
                output = validate_output_path(output, input_path=media_path,
                    sidecars=(*protected, *outputs), overwrite=config.overwrite)
            source_output = None
            if config.save_source is not None:
                source_output = validate_output_path(config.save_source,
                    input_path=media_path, sidecars=(*protected, output),
                    overwrite=config.overwrite)

            if source is None:
                progress("Transcribing audio")
                source = transcribe(media_path, config.stt, scratch, cancellation, progress)
                source.validate()  # Inferred timing must never opt into overlap.
            else:
                source.validate(allow_overlaps=source.allow_overlaps)
            cancellation.raise_if_cancelled()
            if not source.segments:
                progress("No speech found; no subtitles written")
                return RunResult(None, silent=True)

            if source.language.lower().split("-")[0] == "zh":
                target = source
                progress("Using suitable Chinese subtitles")
            else:
                glossary = read_glossary(config.glossary) if config.glossary is not None else None
                progress("Translating subtitles to Chinese")
                context = ({"media_context": f"{media_path.parent.name} / {media_path.stem}"}
                           if config.include_media_context else {})
                target = translate(source, config.llm, glossary, cancellation, progress, **context)
                validate_translation(source, target)
            cancellation.raise_if_cancelled()

            # Serialize both before committing either: malformed export data
            # cannot leave a newly published target behind.
            target_text = serialize_srt(target, allow_overlaps=target.allow_overlaps)
            source_text = (serialize_srt(source, allow_overlaps=source.allow_overlaps)
                           if source_output is not None else None)

            def publish(path, text, transcript, other_outputs):
                return atomic_publish(path, text, input_path=media_path,
                    sidecars=(*protected, *sidecar_paths(media_path), *other_outputs),
                    overwrite=config.overwrite,
                    cancellation=cancellation,
                    validator=lambda data: parse_srt(data.decode("utf-8"),
                        language=transcript.language, allow_overlaps=transcript.allow_overlaps))

            if not already_present:
                output = publish(output, target_text, target, outputs)
                progress("Chinese subtitles written")
            if source_output is not None:
                source_output = publish(source_output, source_text, source, (output,))
                progress("Source subtitles written")
            return RunResult(output, source_output)
    except OSError:
        raise ProcessingError("Cannot read media or write subtitles; check files and permissions") from None


def _selected_target(output: Path, source: TimedTranscript | None) -> bool:
    """Only an explicitly selected, exact Chinese sidecar may satisfy output."""
    if source is None or source.language.lower().split("-")[0] != "zh":
        return False
    selected = source.provenance.get("source_path")
    if source.provenance.get("source_kind") != "sidecar" or not isinstance(selected, str):
        return False
    output = Path(output)
    if (output.suffix.lower() != ".srt" or output.is_symlink() or not output.is_file()
            or output.absolute() != Path(selected).absolute()):
        return False
    current = parse_srt(output.read_text(encoding="utf-8-sig"), language=source.language,
                        allow_overlaps=source.allow_overlaps)
    if current.segments != source.segments:
        raise ValidationError("Selected Chinese sidecar changed during discovery")
    return True


def validate_translation(source: TimedTranscript, target: TimedTranscript) -> None:
    """Reject adapter results that change the source timeline or cue identity."""
    source.validate(allow_overlaps=source.allow_overlaps)
    target.validate(allow_overlaps=target.allow_overlaps)
    if not target.segments or target.language != "zh":
        raise ValidationError("Translation must contain nonempty Chinese subtitles")
    if target.allow_overlaps != source.allow_overlaps or tuple(
        (cue.id, cue.start, cue.end) for cue in target.segments
    ) != tuple((cue.id, cue.start, cue.end) for cue in source.segments):
        raise ValidationError("Translation changed source cue identities or timing")
