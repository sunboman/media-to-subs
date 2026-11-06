import dataclasses
import math
import unittest

from media_to_subs.errors import ValidationError
from media_to_subs.subtitles import parse_srt, serialize_srt
from media_to_subs.types import Segment, TimedTranscript


class TranscriptTests(unittest.TestCase):
    def test_immutable_snapshot_including_nested_provenance(self):
        segments = [Segment("cue-a", 0, 1, "Synthetic line")]
        provenance = {"engine": "fixture", "options": {"streams": [1]}}
        transcript = TimedTranscript("ja", segments, provenance)
        segments.clear()
        provenance["options"]["streams"].append(2)
        self.assertEqual(transcript.provenance["options"]["streams"], (1,))
        self.assertEqual(len(transcript.segments), 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            transcript.segments[0].text = "changed"
        with self.assertRaises(TypeError):
            transcript.provenance["engine"] = "changed"

    def test_reject_invalid_segment_content_and_times(self):
        for start, end in [(math.nan, 1), (0, math.inf), (-1, 1), (1, 1),
                           (2, 1), (True, 2), ("0", 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                Segment("a", start, end, "line")
        for cue_id, text in [("", "x"), ("bad id", "x"), (1, "x"),
                             ("a", "  \n"), ("a", "a\x00b")]:
            with self.subTest(id=cue_id, text=text), self.assertRaises(ValidationError):
                Segment(cue_id, 0, 1, text)

    def test_strict_timing_unique_ids_and_explicit_authored_overlap(self):
        a = Segment("a", 0, 2, "A")
        b = Segment("b", 1, 3, "B")
        for cues in [(a, b), (b, a), (a, Segment("a", 3, 4, "C"))]:
            with self.assertRaises(ValidationError):
                TimedTranscript("ja", cues, {})
        authored = TimedTranscript("ja", (a, b), {}, allow_overlaps=True)
        with self.assertRaises(ValidationError):
            authored.validate()  # Strict by default even for authored cues.
        authored.validate(allow_overlaps=True)
        with self.assertRaises(ValidationError):
            TimedTranscript("ja", (b, a), {}, allow_overlaps=True)

    def test_empty_transcript_represents_silence_and_duration_is_checked(self):
        self.assertEqual(TimedTranscript("ja", (), {}).segments, ())
        transcript = TimedTranscript("ja", (Segment("a", 0, 2, "A"),), {})
        with self.assertRaises(ValidationError):
            transcript.validate(duration=1)


class SRTTests(unittest.TestCase):
    def test_round_trip_preserves_numbering_multiline_and_timing(self):
        source = "7\n00:00:01,234 --> 00:00:02,500\nSynthetic first\nSecond line\n\n12\n01:02:03,000 --> 01:02:04,001\n次の例\n"
        transcript = parse_srt("\ufeff" + source.replace("\n", "\r\n"), language="ja")
        self.assertEqual([s.id for s in transcript.segments], ["7", "12"])
        self.assertEqual(transcript.segments[0].text, "Synthetic first\nSecond line")
        self.assertEqual(serialize_srt(transcript), source)
        self.assertEqual(parse_srt(serialize_srt(transcript), language="ja").segments,
                         transcript.segments)

    def test_opaque_ids_number_in_order_and_round_half_up(self):
        transcript = TimedTranscript("ja", (Segment("chunk-1", 0.0005, 1.0005, "A"),), {})
        self.assertEqual(serialize_srt(transcript), "1\n00:00:00,001 --> 00:00:01,001\nA\n")

    def test_rejects_malformed_or_unsupported_payloads(self):
        invalid = ["", "plain text", "WEBVTT\n", "1\n00:60:00,000 --> 00:60:01,000\nX",
                   "1\n00:00:01,000 --> 00:00:01,000\nX",
                   "1\n00:00:00.000 --> 00:00:01.000\nX",
                   "1\n00:00:00,000 --> 00:00:01,000 position:10%\nX",
                   "0\n00:00:00,000 --> 00:00:01,000\nX",
                   "1\n00:00:00,000 --> 00:00:01,000\n",
                   "1\n00:00:00,000 --> 00:00:01,000\nX\n\n1\n00:00:02,000 --> 00:00:03,000\nY"]
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(ValidationError):
                parse_srt(source, language="ja")

    def test_authored_overlap_requires_explicit_option(self):
        source = "1\n00:00:00,000 --> 00:00:02,000\nA\n\n2\n00:00:01,000 --> 00:00:03,000\nB\n"
        with self.assertRaises(ValidationError):
            parse_srt(source, language="ja")
        transcript = parse_srt(source, language="ja", allow_overlaps=True)
        with self.assertRaises(ValidationError):
            serialize_srt(transcript)
        self.assertEqual(serialize_srt(transcript, allow_overlaps=True), source)

    def test_internal_blank_lines_and_numeric_dialogue_survive_round_trip(self):
        source = ("1\n00:00:00,100 --> 00:00:02,000\nFirst synthetic line\n\n"
                  "Second synthetic line\n\n42\n\n"
                  "2\n00:00:02,100 --> 00:00:03,800\nA separate cue\n")
        transcript = parse_srt(source, language="en")
        self.assertEqual(len(transcript.segments), 2)
        self.assertEqual(transcript.segments[0].text,
                         "First synthetic line\n\nSecond synthetic line\n\n42")
        self.assertEqual(serialize_srt(transcript), source)
        self.assertEqual(parse_srt(source.replace("\n", "\r")).segments, transcript.segments)

    def test_malformed_headers_are_not_swallowed_as_blank_line_continuation(self):
        prefix = "1\n00:00:00,000 --> 00:00:01,000\nFirst cue\n\n"
        for bad in ("2\n00:00:02.000 --> 00:00:03.000\nBad timestamp",
                    "missing-index\n00:00:02,000 --> 00:00:03,000\nBad index",
                    "00:00:02,000 --> 00:00:03,000\nMissing index",
                    "2\n00:00:02,000 00:00:03,000\nMissing arrow"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                parse_srt(prefix + bad)

    def test_serialization_rejects_ambiguous_headers_and_collapsed_cues(self):
        for segment in [Segment("a", 0, 1, "a\n\n2\n00:00:02,000 --> 00:00:03,000\nb"),
                        Segment("a", 0, 1, "\na"), Segment("a", 0, 0.0001, "a")]:
            with self.subTest(segment=segment), self.assertRaises(ValidationError):
                serialize_srt(TimedTranscript("ja", (segment,), {}))


if __name__ == "__main__":
    unittest.main()
