"""Synthetic regressions for Whisper timing and speech-region recovery."""
import tempfile
import unittest
import wave
from pathlib import Path

from media_to_subs import asr_timing as timing
from media_to_subs.asr_quality import block_times_ms, ms_to_timestamp, require_quality, TranscriptionQualityError

MAPPING = (Path(__file__).parent / 'fixtures/asr/vad-synthetic.txt').read_text()


def block(start, end, text='合成テストの文章です'):
    return {'index': '1', 'time': f'{ms_to_timestamp(start)} --> {ms_to_timestamp(end)}', 'text': [text]}


def wav(path, seconds):
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        writer.writeframes(b'\0\0' * round(16000 * seconds))


class TimingTests(unittest.TestCase):
    def test_gap_repair_retracts_only_removed_silence(self):
        original = [block(2000, 7000), block(7000, 12000), block(13000, 15000)]
        repaired = timing.repair_vad_timing(original, MAPPING)
        self.assertEqual([block_times_ms(b) for b in repaired], [(2000, 4300), (9800, 12000), (13000, 15000)])
        self.assertEqual(block_times_ms(original[0]), (2000, 7000))
        self.assertEqual([b['text'] for b in original], [b['text'] for b in repaired])

    def test_unknown_mapping_and_ambiguous_text_never_invent_alignment(self):
        for mapping in ('', MAPPING.replace('detected 3', 'detected 4'), MAPPING.replace('vad_start: 2.20', 'vad_start: 2.80')):
            self.assertIsNone(timing.validated_vad_segments(mapping))
        for original in ([block(6000, 8000)], [block(3000, 11000)]):
            self.assertEqual(timing.repair_vad_timing(original, MAPPING), original)

    def test_nested_offsets_are_applied_once(self):
        repaired = timing.repair_vad_timing([block(7000, 12000)], MAPPING)
        result = timing.merge_chunk_transcripts([(timing.AudioChunk(Path('r'), 30000, 20000), repaired)], 0)
        result = timing.merge_chunk_transcripts([(timing.AudioChunk(Path('p'), 60000, 50000), result)], 0)
        self.assertEqual(block_times_ms(result[0]), (99800, 102000))

    def test_duplicate_shared_context_keeps_complete_actual_inference(self):
        left, right = timing.AudioChunk(Path('a'), 0, 30000), timing.AudioChunk(Path('b'), 28000, 30000)
        short = 'This synthetic caption has a complete'
        complete = short + ' continuation.'
        result = timing.merge_chunk_transcripts([(left, [block(27790, 30000, short)]), (right, [block(0, 3440, complete)])], 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['text'], [complete])
        self.assertEqual(block_times_ms(result[0]), (27790, 31440))

    def test_distinct_or_short_repeated_speech_is_not_deleted(self):
        for texts in [('はい', 'はい'), ('one unrelated synthetic statement', 'another unrelated synthetic statement')]:
            results = [(timing.AudioChunk(Path('a'), 0, 30000), [block(27790, 30000, texts[0])]), (timing.AudioChunk(Path('b'), 28000, 30000), [block(0, 3440, texts[1])])]
            self.assertEqual(len(timing.merge_chunk_transcripts(results, 2)), 2)

    def test_parent_coverage_requires_every_original_speech_region(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audio.wav'
            wav(path, 20)
            with self.assertRaises(TranscriptionQualityError):
                timing.require_original_region_timing(path, [block(2000, 4000), block(10000, 12000)], MAPPING)
            timing.require_original_region_timing(path, [block(2000, 4000), block(10000, 12000), block(13000, 15000)], MAPPING)

    def test_reinference_uses_original_pcm_and_offsets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audio.wav'
            wav(path, 20)
            seen = []
            def infer(audio, prefix):
                with wave.open(str(audio), 'rb') as reader:
                    seen.append(reader.getnframes())
                return [block(200, 2200)] if len(seen) == 1 else [block(200, 2200), block(3200, 5200)]
            result = timing.infer_original_regions(path, Path(temp)/'regions', MAPPING, infer)
            self.assertEqual(seen, [40000, 88000])
            self.assertEqual([block_times_ms(b) for b in result], [(2000, 4000), (10000, 12000), (13000, 15000)])

    def test_missing_region_content_fails_and_endpoint_tolerance_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audio.wav'
            wav(path, 20)
            with self.assertRaises(TranscriptionQualityError):
                timing.infer_original_regions(path, Path(temp)/'regions', MAPPING, lambda *_: [])
            self.assertEqual(timing.validate_vad_audio(path, [(0, 20040, 0, 20000)]), 20000)
            with self.assertRaises(TranscriptionQualityError):
                timing.validate_vad_audio(path, [(0, 20041, 0, 20000)])

    def test_chunk_bytes_offsets_and_last_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audio.wav'
            wav(path, 7.125)
            chunks = timing.split_wave(path, Path(temp)/'chunks', 3, 1)
            self.assertEqual([(c.start_ms, c.duration_ms) for c in chunks], [(0,3000),(2000,3000),(4000,3000),(6000,1125)])


class QualityTests(unittest.TestCase):
    def test_repetition_regressions(self):
        for blocks in ([block(0, 1000, 'repeat' * 12)], [block(i*1000, (i+1)*1000, 'repeated phrase') for i in range(12)]):
            with self.assertRaises(TranscriptionQualityError):
                require_quality(blocks)
        require_quality([block(0, 1000, 'hello'), block(1100, 2000, 'world')])


if __name__ == '__main__':
    unittest.main()
