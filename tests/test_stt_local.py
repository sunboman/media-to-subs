import tempfile
import unittest
import wave
from pathlib import Path

from media_to_subs.asr.common import bound_blocks, is_quiet_wave, transcript_from_blocks, wave_duration
from media_to_subs.asr_quality import TranscriptionQualityError
from test_asr_timing import block, wav


class InferenceBoundsTests(unittest.TestCase):
    def test_silence_is_measured_independently_of_empty_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'audio.wav'
            wav(path, 1.125)
            self.assertTrue(is_quiet_wave(path))
            self.assertEqual(wave_duration(path), 1.125)
            with wave.open(str(path), 'wb') as writer:
                writer.setparams((1,2,16000,0,'NONE','not compressed'))
                writer.writeframes(b'\x10\x00' * 16000)
            self.assertFalse(is_quiet_wave(path))

    def test_local_bounds_reject_overlap_overrun_and_retract_only_rounding(self):
        self.assertEqual(bound_blocks([block(0, 1130)], 1.125)[0]['time'], '00:00:00,000 --> 00:00:01,125')
        for blocks in ([block(0, 1150)], [block(1000, 1300)], [block(0, 800), block(600, 1000)]):
            with self.assertRaises(TranscriptionQualityError):
                bound_blocks(blocks, 1.125)

    def test_final_cues_use_physical_duration_and_mapping_provenance(self):
        transcript = transcript_from_blocks([block(0, 1126)], 'ja', 1.1256, {'engine':'whisper-local', 'vad_enabled':False})
        self.assertEqual(transcript.segments[0].end, 1.1256)
        self.assertFalse(transcript.provenance['vad_enabled'])
        transcript.validate(duration=1.1256)


class LocalRecoveryTests(unittest.TestCase):
    def test_detect_parser_prefers_detected_language_over_requested_auto(self):
        from media_to_subs.asr.local import parse_detect_language_output
        self.assertEqual(parse_detect_language_output('language = auto\nauto-detected language: ja (p = 0.98)'), 'ja')
        self.assertEqual(parse_detect_language_output('Detected language: korean'), 'ko')
        for output in ('Detected language: en', 'whisper_print_timings: sample time = 0.00 ms'):
            with self.assertRaises(TranscriptionQualityError):
                parse_detect_language_output(output)

    def test_failed_speech_cannot_recover_as_all_silence(self):
        from media_to_subs.asr.local import recover_chunk
        from media_to_subs.asr_timing import AudioChunk
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'audio.wav'
            wav(path, 32)
            count = 0
            def infer(*args):
                nonlocal count
                count += 1
                if count == 1:
                    raise TranscriptionQualityError('failed speech')
                return []
            with self.assertRaises(TranscriptionQualityError):
                recover_chunk(AudioChunk(path, 0, 32000), Path(temp)/'work', infer)

    def test_minimum_recovery_never_discards_looped_caption(self):
        from media_to_subs.asr.local import recover_chunk
        from media_to_subs.asr_timing import AudioChunk
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'audio.wav'
            wav(path, 10)
            with self.assertRaises(TranscriptionQualityError):
                recover_chunk(AudioChunk(path, 0, 10000), Path(temp)/'work', lambda *_: [block(0, 1000, 'repeat'*20)])

    def test_subdivision_must_keep_parent_vad_coverage(self):
        from media_to_subs.asr.local import OriginalRegionQualityError, recover_chunk
        from media_to_subs.asr_timing import AudioChunk
        from test_asr_timing import MAPPING
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'audio.wav'
            wav(path, 32)
            count = 0
            def infer(*args):
                nonlocal count
                count += 1
                if count == 1:
                    raise OriginalRegionQualityError('parent region failed', MAPPING)
                return [block(1000, 2000)]
            with self.assertRaises(TranscriptionQualityError):
                recover_chunk(AudioChunk(path, 60000, 32000), Path(temp)/'work', infer)

    def test_recovery_offsets_applied_once_and_cancellation_propagates(self):
        from media_to_subs.asr.local import recover_chunk
        from media_to_subs.asr_timing import AudioChunk
        from media_to_subs.asr_quality import block_times_ms
        from media_to_subs.errors import CancelledError
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'audio.wav'
            wav(path, 32)
            count = 0
            def infer(*args):
                nonlocal count
                count += 1
                if count == 1:
                    raise TranscriptionQualityError('retry')
                return [block(3000, 4000)]
            result = recover_chunk(AudioChunk(path, 60000, 32000), Path(temp)/'work', infer)
            self.assertEqual([block_times_ms(b) for b in result], [(3000,4000),(17000,18000),(31000,32000)])
            def cancel():
                raise CancelledError('cancelled')
            with self.assertRaises(CancelledError):
                recover_chunk(AudioChunk(path, 0, 32000), Path(temp)/'other', infer, check_cancelled=cancel)


class LocalExecutableTests(unittest.TestCase):
    def setUp(self):
        from media_to_subs.config import STTConfig
        from media_to_subs.runtime import Cancellation
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio = self.root/'audio.wav'
        with wave.open(str(self.audio),'wb') as writer:
            writer.setparams((1,2,16000,0,'NONE','not compressed'))
            writer.writeframes(b'\x40\x00'*16000*20)
        self.model = self.root/'model.bin'
        self.model.write_bytes(b'fixture model')
        self.binary = self.root/'fake-whisper'
        self.config = STTConfig(model_path=self.model, whisper_binary=str(self.binary), source_language='ja')
        self.cancel = Cancellation()

    def tearDown(self): self.temp.cleanup()

    def fake(self, body):
        import sys
        self.binary.write_text('#!' + sys.executable + '\nimport sys,time\nfrom pathlib import Path\nargs=sys.argv\n'+body)
        self.binary.chmod(0o700)

    def test_vad_repairs_original_mapping_and_records_no_invented_words(self):
        from dataclasses import replace
        from media_to_subs.asr.local import run_whisper
        from media_to_subs.asr_quality import block_times_ms
        from test_asr_timing import MAPPING
        raw = '1\n00:00:02,000 --> 00:00:07,000\nこれは最初の合成字幕です\n\n2\n00:00:07,000 --> 00:00:12,000\nこれは次の合成字幕です\n\n3\n00:00:13,000 --> 00:00:15,000\nこれは最後の合成字幕です\n'
        self.fake(f'Path(args[args.index("-of")+1]+".srt").write_text({raw!r})\nprint({MAPPING!r},file=sys.stderr)\n')
        blocks = run_whisper(self.audio,self.root/'result',replace(self.config,vad_model=self.model),'ja',self.cancel)
        self.assertEqual([block_times_ms(b) for b in blocks],[(2000,4300),(9800,12000),(13000,15000)])

    def test_no_vad_empty_output_is_failure_and_user_sidecar_survives(self):
        from media_to_subs.asr.local import run_whisper
        sidecar = self.root/'audio.ja.srt'
        sidecar.write_bytes(b'authored unchanged')
        self.fake('Path(args[args.index("-of")+1]+".srt").write_text("")\n')
        with self.assertRaises(TranscriptionQualityError):
            run_whisper(self.audio,self.root/'result',self.config,'ja',self.cancel)
        self.assertEqual(sidecar.read_bytes(),b'authored unchanged')

    def test_validated_vad_zero_is_distinct_from_failed_inference(self):
        from dataclasses import replace
        from media_to_subs.asr.local import run_whisper
        self.fake('Path(args[args.index("-of")+1]+".srt").write_text("")\nprint("whisper_vad: detected 0 speech segments",file=sys.stderr)\n')
        self.assertEqual(run_whisper(self.audio,self.root/'result',replace(self.config,vad_model=self.model),'ja',self.cancel),[])

    def test_existing_scratch_output_is_not_unlinked_or_reused(self):
        from media_to_subs.asr.local import run_whisper
        from media_to_subs.errors import ProcessingError
        existing = self.root/'result.srt'
        existing.write_text('preserve')
        self.fake('raise RuntimeError("must not run")\n')
        with self.assertRaises(ProcessingError):
            run_whisper(self.audio,self.root/'result',self.config,'ja',self.cancel)
        self.assertEqual(existing.read_text(),'preserve')

    def test_bounded_process_timeout_and_cancellation(self):
        from dataclasses import replace
        from media_to_subs.asr.local import run_whisper
        from media_to_subs.errors import ProcessTimeoutError, CancelledError
        import threading
        import time
        self.fake('time.sleep(20)\n')
        start = time.monotonic()
        with self.assertRaises(ProcessTimeoutError):
            run_whisper(self.audio,self.root/'result',replace(self.config,process_timeout=.1),'ja',self.cancel)
        self.assertLess(time.monotonic()-start,2)
        timer = threading.Timer(.1,self.cancel.cancel)
        timer.start()
        try:
            with self.assertRaises(CancelledError):
                run_whisper(self.audio,self.root/'result2',self.config,'ja',self.cancel)
        finally: timer.cancel()
