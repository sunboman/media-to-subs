import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from media_to_subs.asr import transcribe
from media_to_subs.config import STTConfig
from media_to_subs.runtime import Cancellation


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scratch = self.root/'scratch'
        self.scratch.mkdir()
        self.audio = self.root/'input.wav'

    def tearDown(self): self.temp.cleanup()

    def make_audio(self, samples):
        with wave.open(str(self.audio),'wb') as writer:
            writer.setparams((1,2,16000,0,'NONE','not compressed'))
            writer.writeframes(samples)

    def test_quiet_audio_needs_no_model_or_http_request_and_reports_actual_checks(self):
        self.make_audio(b'\0\0'*16000)
        for mode in ('api','local'):
            with self.subTest(mode=mode):
                config = STTConfig(mode=mode,whisper_binary='missing-unused',source_language='ja')
                result = transcribe(self.audio,config,self.scratch,Cancellation(),lambda _:None)
                self.assertEqual(result.segments,())
                self.assertEqual(result.provenance['engine'],'pcm-silence')
                self.assertFalse(result.provenance['vad_enabled'])
                self.assertEqual(list(self.scratch.iterdir()),[])

    def test_local_failure_never_calls_api_or_modifies_sidecar(self):
        self.make_audio(b'\x40\x00'*16000)
        sidecar = self.root/'input.ja.srt'
        sidecar.write_bytes(b'authored preserve')
        with patch('media_to_subs.asr.api.transcribe_audio',side_effect=AssertionError('API called')):
            with self.assertRaises(Exception):
                transcribe(self.audio,STTConfig(source_language='ja'),self.scratch,Cancellation(),lambda _:None)
        self.assertEqual(sidecar.read_bytes(),b'authored preserve')
        self.assertEqual(list(self.scratch.iterdir()),[])
