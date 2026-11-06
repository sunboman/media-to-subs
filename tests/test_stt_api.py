import json
import math
import unittest

from media_to_subs.asr.api import parse_verbose_json, transcription_url
from media_to_subs.asr_quality import TranscriptionQualityError, block_times_ms


class ApiParsingTests(unittest.TestCase):
    def payload(self, segments=None, **fields):
        return json.dumps({'language': 'japanese', 'duration': 99999,
                           'segments': segments if segments is not None else [{'start': 0, 'end': 1, 'text': 'こんにちは'}], **fields})

    def test_measured_duration_and_language_alias(self):
        language, blocks = parse_verbose_json(self.payload(), duration=2, language='auto')
        self.assertEqual(language, 'ja')
        self.assertEqual(block_times_ms(blocks[0]), (0, 1000))
        with self.assertRaises(TranscriptionQualityError):
            parse_verbose_json(self.payload([{'start': 0, 'end': 3, 'text': 'こんにちは'}]), duration=2, language='ja')

    def test_modest_endpoint_rounding_only(self):
        for end in (2.001, 2.019, 2.02):
            _, blocks = parse_verbose_json(self.payload([{'start': 1, 'end': end, 'text': 'こんにちは'}]), duration=2, language='ja')
            self.assertEqual(block_times_ms(blocks[0]), (1000, 2000))
        with self.assertRaises(TranscriptionQualityError):
            parse_verbose_json(self.payload([{'start': 0, 'end': 2.021, 'text': 'こんにちは'}]), duration=2, language='ja')

    def test_invalid_timestamps_text_or_overlap_rejected(self):
        invalid = [
            [{'start': 0, 'end': math.nan, 'text': 'x'}],
            [{'start': 0, 'end': math.inf, 'text': 'x'}],
            [{'start': 0, 'end': 10**400, 'text': 'テストです'}],
            [{'start': 0, 'end': 1e308, 'text': 'テストです'}],
            [{'start': 10**400, 'end': 2, 'text': 'テストです'}],
            [{'start': -0.001, 'end': 1, 'text': 'x'}],
            [{'start': True, 'end': 2, 'text': 'x'}],
            [{'start': '0', 'end': 1, 'text': 'x'}],
            [{'start': 1, 'end': 1, 'text': 'x'}],
            [{'end': 1, 'text': 'x'}],
            [{'start': 0, 'end': 1, 'text': ''}],
            [{'start': 0, 'end': 1, 'text': ['x']}],
            [{'start': 0, 'end': 1.5, 'text': 'x'}, {'start': 1, 'end': 2, 'text': 'y'}],
        ]
        for segments in invalid:
            with self.subTest(segments=segments), self.assertRaises(TranscriptionQualityError):
                parse_verbose_json(self.payload(segments), duration=2, language='ja')

    def test_invalid_or_oversized_measured_duration_is_a_quality_error(self):
        for duration in (10**400, -10**400, 1e308, math.inf, math.nan, True, None, "3"):
            with self.subTest(type=type(duration).__name__), self.assertRaises(TranscriptionQualityError):
                parse_verbose_json(self.payload(), duration=duration, language='ja')

    def test_missing_segments_text_only_empty_and_wrong_language_rejected(self):
        for payload in ('plain text', '{"text":"hello"}', self.payload([]), self.payload(language='French'), self.payload(language='korean'), self.payload(text='missing additional text')):
            with self.subTest(payload=payload), self.assertRaises(TranscriptionQualityError):
                parse_verbose_json(payload, duration=2, language='ja')
        self.assertEqual(parse_verbose_json(self.payload([], text=''), duration=2, language='ja', quiet=True), ('ja', []))

    def test_url_append_and_full_url(self):
        self.assertEqual(transcription_url('http://localhost:1234/v1/'), 'http://localhost:1234/v1/audio/transcriptions')
        self.assertEqual(transcription_url('https://example.com/v1/audio/transcriptions'), 'https://example.com/v1/audio/transcriptions')
        for url in ('file:///tmp/api', 'https://u:password@example.com/v1', 'https://example.com/v1?key=secret', 'https://example.com/v1#secret'):
            with self.assertRaises(ValueError):
                transcription_url(url)


class ApiRuntimeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import threading
        import wave
        from pathlib import Path
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from media_to_subs.config import STTConfig
        from media_to_subs.runtime import Cancellation
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio = self.root/'input.wav'
        with wave.open(str(self.audio),'wb') as writer:
            writer.setparams((1,2,16000,0,'NONE','not compressed'))
            writer.writeframes(b'\x40\x00'*16000*5)
        self.requests, self.statuses, self.delay = [], [], 0
        self.request_received = threading.Event()
        self.response = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                import time
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.requests.append(body)
                owner.request_received.set()
                time.sleep(owner.delay)
                status = owner.statuses.pop(0) if owner.statuses else 200
                self.send_response(status)
                self.end_headers()
                if owner.response is not None:
                    payload = owner.response
                else:
                    payload = json.dumps({'language':'japanese','duration':999,'text':'こんにちは', 'segments':[{'start':.1,'end':.6,'text':'こんにちは'}]}).encode()
                try: self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.config = STTConfig(mode='api',url=f'http://127.0.0.1:{self.server.server_port}/v1',
                                api_key='fixture-secret',source_language='ja',connect_timeout=1,read_timeout=2,
                                max_retries=1,chunk_duration=2,chunk_overlap=.5,max_upload_bytes=100000)
        self.cancel = Cancellation()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def test_chunk_upload_limits_and_original_offsets(self):
        from media_to_subs.asr.api import transcribe_audio
        language, blocks = transcribe_audio(self.audio,self.config,self.root,self.cancel,lambda _:None)
        self.assertEqual(language,'ja')
        self.assertEqual([block_times_ms(b) for b in blocks],[(100,600),(1600,2100),(3100,3600)])
        self.assertEqual(len(self.requests),3)
        self.assertTrue(all(len(body)<=100000 for body in self.requests))

    def test_transient_retry_only_and_no_raw_error_body(self):
        from dataclasses import replace
        from media_to_subs.asr.api import request_chunk
        from media_to_subs.errors import ProviderError
        config = replace(self.config,max_upload_bytes=24000000)
        self.statuses = [503,200]
        self.assertIn('segments',request_chunk(self.audio,config,'ja',self.cancel))
        self.assertEqual(len(self.requests),2)
        self.requests.clear()
        self.statuses = [401]
        self.response = b'fixture-secret remote error body'
        with self.assertRaises(ProviderError) as caught:
            request_chunk(self.audio,config,'ja',self.cancel)
        self.assertEqual(len(self.requests),1)
        self.assertNotIn('fixture-secret',str(caught.exception))

    def test_invalid_payload_is_not_retried(self):
        from media_to_subs.asr.api import transcribe_audio
        self.response = b'{"text":"no timestamps"}'
        with self.assertRaises(TranscriptionQualityError):
            transcribe_audio(self.audio,self.config,self.root,self.cancel,lambda _:None)
        self.assertEqual(len(self.requests),1)

    def test_cancellation_during_http_read_is_bounded(self):
        import threading
        import time
        from dataclasses import replace
        from media_to_subs.asr.api import request_chunk
        from media_to_subs.errors import CancelledError
        self.delay = 5
        config = replace(self.config,max_upload_bytes=24000000,read_timeout=30)
        cancelled_at = []
        def cancel_during_read():
            if self.request_received.wait(10):
                cancelled_at.append(time.monotonic())
            self.cancel.cancel()
        watcher = threading.Thread(target=cancel_during_read)
        watcher.start()
        try:
            with self.assertRaises(CancelledError):
                request_chunk(self.audio,config,'ja',self.cancel)
        finally:
            watcher.join(11)
        self.assertEqual(len(cancelled_at), 1, "No HTTP request observed before cancellation")
        self.assertLess(time.monotonic()-cancelled_at[0],2)
        self.assertEqual(len(self.requests),1)

    def test_read_timeout_bounded_and_no_retry_when_disabled(self):
        import time
        from dataclasses import replace
        from media_to_subs.asr.api import request_chunk
        from media_to_subs.errors import ProviderError
        self.delay = 3
        start = time.monotonic()
        with self.assertRaises(ProviderError):
            request_chunk(self.audio,replace(self.config,max_upload_bytes=24000000,read_timeout=.1,max_retries=0),'ja',self.cancel)
        self.assertLess(time.monotonic()-start,2)
        self.assertEqual(len(self.requests),1)
