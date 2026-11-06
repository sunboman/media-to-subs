"""Local HTTP transport fixtures; no external provider requests."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKER = Path(__file__).parents[1]/'src/media_to_subs/asr/_http_worker.py'


class HttpWorkerTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.requests.append((self.path, dict(self.headers), body))
                status = 307 if self.path == '/redirect' else 200
                self.send_response(status)
                if status == 307:
                    self.send_header('Location', '/credential-leak')
                self.end_headers()
                self.wfile.write(b'{"language":"japanese","segments":[{"start":0,"end":1,"text":"synthetic"}]}')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.audio = Path(self.temp.name)/'private-title.wav'
        with wave.open(str(self.audio), 'wb') as writer:
            writer.setparams((1,2,16000,0,'NONE','not compressed'))
            writer.writeframes(b'\0\0'*16000)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path='/v1/audio/transcriptions', **kwargs):
        request = {'url': f'http://127.0.0.1:{self.server.server_port}{path}', 'api_key': 'fixture-secret',
                   'model': 'whisper-1', 'response_format': 'verbose_json', 'language': 'ja',
                   'audio_path': str(self.audio), 'max_upload_bytes': 24000000,
                   'connect_timeout': 1, 'read_timeout': 1}
        request.update(kwargs)
        result = subprocess.run([sys.executable, str(WORKER)], input=json.dumps(request), text=True, capture_output=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('fixture-secret', result.stdout + result.stderr)
        self.assertNotIn('private-title', result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_multipart_contract_has_segment_timestamps_and_generic_filename(self):
        response = self.request()
        self.assertEqual(response['status'], 200)
        path, headers, body = self.requests[0]
        self.assertEqual(path, '/v1/audio/transcriptions')
        self.assertEqual(headers['Authorization'], 'Bearer fixture-secret')
        for value in (b'name="file"; filename="audio.wav"', b'name="model"', b'whisper-1',
                      b'name="response_format"', b'verbose_json', b'name="timestamp_granularities[]"',
                      b'segment', b'name="language"', b'ja', b'Content-Type: audio/wav'):
            self.assertIn(value, body)
        self.assertNotIn(b'private-title', body)

    def test_redirect_is_not_followed(self):
        self.assertEqual(self.request('/redirect')['status'], 307)
        self.assertEqual(len(self.requests), 1)

    def test_auto_language_omits_hint(self):
        self.request(language='auto')
        self.assertNotIn(b'name="language"', self.requests[0][2])

    def test_request_byte_limit_applies_before_network(self):
        self.assertEqual(self.request(max_upload_bytes=100)['error'], 'request')
        self.assertEqual(self.requests, [])

    def test_bad_url_and_header_are_safe(self):
        for override in ({'url': 'https://user:secret@example.invalid/transcriptions'}, {'api_key': 'a\r\nInjected: yes'}):
            self.assertEqual(self.request(**override)['error'], 'request')
        self.assertEqual(self.requests, [])
