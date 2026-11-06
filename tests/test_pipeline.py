"""Pipeline regressions use real timed text and file safety, fake inference only."""

from dataclasses import replace
from contextlib import ExitStack, chdir
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_to_subs.config import LLMConfig, RunConfig, STTConfig
from media_to_subs.errors import CancelledError, FileSafetyError, ProviderError, ValidationError
from media_to_subs.runtime import Cancellation
from media_to_subs.subtitles import parse_srt, serialize_srt
from media_to_subs.types import Segment, TimedTranscript
from media_to_subs.pipeline import run, validate_translation


def transcript(language="ja", *, overlap=False):
    return TimedTranscript(language, (
        Segment("7", 0.125, 1.5, "こんにちは"),
        Segment("19", 1.25 if overlap else 2.0, 3.875, "ありがとう"),
    ), {"origin": "synthetic"}, allow_overlaps=overlap)


class TranslationBoundaryTests(unittest.TestCase):
    def test_accepts_exact_ids_times_and_authored_overlap(self):
        source = transcript(overlap=True)
        target = replace(source, language="zh", segments=tuple(
            replace(cue, text="你好") for cue in source.segments))
        validate_translation(source, target)

    def test_rejects_changed_cue_identity_count_timing_or_overlap_policy(self):
        source = transcript()
        for target in (
            replace(source, language="zh", segments=source.segments[:1]),
            replace(source, language="zh", segments=(replace(source.segments[0], id="8"), source.segments[1])),
            replace(source, language="zh", segments=(replace(source.segments[0], start=0.126), source.segments[1])),
            replace(source, language="zh", segments=(replace(source.segments[0], end=1.6), source.segments[1])),
            replace(source, language="zh", allow_overlaps=True),
            replace(source, language="en"),
        ):
            with self.subTest(target=target), self.assertRaises(ValidationError):
                validate_translation(source, target)

    def test_rejects_silence_as_translation(self):
        with self.assertRaises(ValidationError):
            validate_translation(transcript(), TimedTranscript("zh", (), {}))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.media = self.root / "input.wav"
        self.media.write_bytes(b"synthetic input preserved by orchestration")
        self.sidecar = self.root / "input.en.srt"
        self.sidecar.write_bytes(b"unselected malformed authored sidecar")
        self.unrelated = self.root / "unrelated.txt"
        self.unrelated.write_bytes(b"unrelated file")
        self.originals = self.hashes()
        self.output = self.root / "translated.srt"
        self.cancel = Cancellation()
        self.progress = []
        self.scratch_paths = []
        self.discover = self.stack.enter_context(patch("media_to_subs.media.discover", return_value=None))
        self.stt = self.stack.enter_context(patch("media_to_subs.asr.transcribe", side_effect=self.transcribe))
        self.llm = self.stack.enter_context(patch("media_to_subs.translator.translate", side_effect=self.translate))

    def hashes(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.media, self.sidecar, self.unrelated)}

    def transcribe(self, media, config, scratch, cancellation, progress):
        self.scratch_paths.append(scratch)
        (scratch / "owned.wav").write_bytes(b"owned scratch")
        return transcript()

    def translate(self, source, config, glossary, cancellation, progress, **kwargs):
        return replace(source, language="zh", segments=tuple(
            replace(cue, text="你好") for cue in source.segments))

    def run_pipeline(self, **kwargs):
        return run(self.media, RunConfig(output=self.output, **kwargs), self.cancel, self.progress.append)

    def assert_preserved(self):
        self.assertEqual(self.hashes(), self.originals)
        self.assertTrue(all(not path.exists() for path in self.scratch_paths))

    def test_transcribe_translate_export_keep_timing_and_cleanup(self):
        export = self.root / "source.srt"
        result = self.run_pipeline(save_source=export)
        target = parse_srt(self.output.read_text(), language="zh")
        source = parse_srt(export.read_text(), language="ja")
        self.assertEqual([(c.id, c.start, c.end) for c in target.segments],
                         [(c.id, c.start, c.end) for c in source.segments])
        self.assertEqual(result.output, self.output)
        self.assertEqual(result.source_output, export)
        self.assert_preserved()

    def test_source_discovery_skips_stt_and_target_discovery_skips_both(self):
        for language in ("ja", "zh", "zh-tw"):
            with self.subTest(language=language):
                self.discover.return_value = transcript(language)
                self.run_pipeline()
                self.stt.assert_not_called()
                self.assertEqual(self.llm.call_count, 1 if language == "ja" else 0)
                self.output.unlink()
                self.llm.reset_mock()
                self.assert_preserved()

    def test_media_context_is_absent_unless_explicitly_enabled(self):
        self.run_pipeline()
        self.assertEqual(self.llm.call_args.kwargs, {})
        self.output.unlink()
        self.run_pipeline(include_media_context=True)
        self.assertEqual(self.llm.call_args.kwargs,
                         {"media_context": f"{self.root.name} / input"})
        self.assertNotIn(str(self.root.parent), self.llm.call_args.kwargs["media_context"])

    def test_relative_media_context_includes_only_actual_parent_name_and_stem(self):
        with chdir(self.root):
            run(Path("input.wav"), RunConfig(output=self.output, include_media_context=True),
                self.cancel, self.progress.append)
        self.assertEqual(self.llm.call_args.kwargs,
                         {"media_context": f"{self.root.name} / input"})

    def test_force_transcribe_bypasses_discovery(self):
        self.discover.return_value = transcript("zh")
        self.run_pipeline(stt=STTConfig(force_transcribe=True))
        self.discover.assert_not_called()
        self.stt.assert_called_once()
        self.llm.assert_called_once()
        self.assert_preserved()

    def test_silence_does_not_invoke_llm_or_publish_either_output(self):
        self.stt.side_effect = None
        self.stt.return_value = TimedTranscript("ja", (), {})
        export = self.root / "source.srt"
        result = self.run_pipeline(save_source=export)
        self.assertTrue(result.silent)
        self.assertIsNone(result.output)
        self.llm.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(export.exists())
        self.assert_preserved()

    def test_conflicting_outputs_and_all_sidecars_fail_before_inference(self):
        for output, export in ((self.media, None), (self.sidecar, None),
                               (self.output, self.output), (self.output, self.sidecar)):
            with self.subTest(output=output, export=export), self.assertRaises(FileSafetyError):
                run(self.media, RunConfig(output=output, save_source=export, overwrite=True),
                    self.cancel, self.progress.append)
        self.stt.assert_not_called()
        self.llm.assert_not_called()
        self.assert_preserved()

    def test_symlink_and_hardlink_aliases_are_rejected(self):
        for alias_kind in ("symlink", "hardlink"):
            with self.subTest(kind=alias_kind):
                if alias_kind == "symlink":
                    self.output.symlink_to(self.sidecar)
                else:
                    self.output.hardlink_to(self.sidecar)
                with self.assertRaises(FileSafetyError):
                    self.run_pipeline(overwrite=True)
                self.output.unlink()
        self.stt.assert_not_called()
        self.assert_preserved()

    def test_explicit_model_assets_and_their_aliases_are_protected(self):
        model = self.root / "model.bin"
        model.write_bytes(b"private model bytes")
        self.output.hardlink_to(model)
        for config in ({"stt": STTConfig(model_path=model)},
                       {"stt": STTConfig(vad_model=model)},
                       {"llm": LLMConfig(model_path=model)}):
            with self.subTest(config=config), self.assertRaises(FileSafetyError):
                self.run_pipeline(overwrite=True, **config)
        self.assertEqual(model.read_bytes(), b"private model bytes")
        self.stt.assert_not_called()
        self.assert_preserved()

    def test_unusable_bitmap_sidecars_remain_protected_with_overwrite(self):
        for suffix in (".en.sub", ".en.idx", ".en.sup"):
            sidecar = self.media.with_suffix(suffix)
            sidecar.write_bytes(b"authored bitmap subtitle bytes")
            with self.subTest(suffix=suffix), self.assertRaises(FileSafetyError):
                run(self.media, RunConfig(output=sidecar, overwrite=True),
                    self.cancel, self.progress.append)
            self.assertEqual(sidecar.read_bytes(), b"authored bitmap subtitle bytes")
        self.stt.assert_not_called()
        self.assert_preserved()

    def test_failure_and_cancel_preserve_old_output_and_authored_inputs(self):
        for failure in (ProviderError("safe failure"), CancelledError("cancelled")):
            with self.subTest(failure=type(failure).__name__):
                self.output.write_bytes(b"previous complete output")
                self.llm.side_effect = failure
                with self.assertRaises(type(failure)):
                    self.run_pipeline(overwrite=True)
                self.assertEqual(self.output.read_bytes(), b"previous complete output")
                self.assert_preserved()

    def test_malformed_translation_never_publishes(self):
        self.llm.side_effect = lambda source, *_: replace(source, language="zh", segments=source.segments[:1])
        with self.assertRaises(ValidationError):
            self.run_pipeline()
        self.assertFalse(self.output.exists())
        self.assert_preserved()

    def test_output_created_during_inference_is_not_clobbered(self):
        def race(source, *args):
            self.output.write_bytes(b"concurrent author's output")
            return self.translate(source, *args)
        self.llm.side_effect = race
        with self.assertRaises(FileSafetyError):
            self.run_pipeline()
        self.assertEqual(self.output.read_bytes(), b"concurrent author's output")
        self.assert_preserved()

    def test_failed_atomic_overwrite_keeps_old_complete_output(self):
        self.output.write_bytes(b"previous complete output")
        with patch("media_to_subs.files.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(FileSafetyError):
                self.run_pipeline(overwrite=True)
        self.assertEqual(self.output.read_bytes(), b"previous complete output")
        self.assertEqual(list(self.root.glob(".media-to-subs-*.tmp")), [])
        self.assert_preserved()

    def test_existing_selected_chinese_destination_is_returned_byte_for_byte(self):
        selected = transcript("zh")
        self.sidecar.write_bytes(serialize_srt(selected).replace("\n", "\r\n").encode("utf-8"))
        self.originals = self.hashes()
        self.discover.return_value = replace(selected, provenance={
            "source_kind": "sidecar", "source_path": str(self.sidecar)})
        result = run(self.media, RunConfig(output=self.sidecar), self.cancel, self.progress.append)
        self.assertEqual(result.output, self.sidecar)
        self.stt.assert_not_called()
        self.llm.assert_not_called()
        self.assert_preserved()


if __name__ == "__main__":
    unittest.main()
