"""One bounded HTTP request in an owned subprocess (stdlib only).

The parent uses shared runtime.run_process so cancellation can terminate DNS,
TLS, uploads and stalled reads. Credentials arrive over stdin, never argv.
There is deliberately no redirect or automatic retry implementation here.
"""
from __future__ import annotations

import http.client
import json
import math
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit
import uuid

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def request(data: dict) -> dict:
    connection = None
    try:
        url = data['url']
        if not isinstance(url, str) or any(ord(c) < 33 for c in url):
            return {'error': 'request'}
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            return {'error': 'request'}
        key = data.get('api_key') or ''
        if not isinstance(key, str) or any(ord(c) < 32 or ord(c) > 126 for c in key):
            return {'error': 'request'}
        connect_timeout, read_timeout = float(data['connect_timeout']), float(data['read_timeout'])
        if not all(math.isfinite(t) and 0 < t <= 86400 for t in (connect_timeout, read_timeout)):
            return {'error': 'request'}
        maximum = data['max_upload_bytes']
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 4096 <= maximum <= 100_000_000:
            return {'error': 'request'}
        audio = Path(data['audio_path'])
        if not audio.is_file() or audio.stat().st_size + 2048 > maximum:
            return {'error': 'request'}
        boundary = 'media-to-subs-' + uuid.uuid4().hex
        fields = [('model', data['model']), ('response_format', data['response_format'])]
        if data['response_format'] == 'verbose_json':
            fields.append(('timestamp_granularities[]', 'segment'))
        elif data['response_format'] != 'srt':
            return {'error': 'request'}
        if data.get('language') in ('ja', 'ko'):
            fields.append(('language', data['language']))
        parts = []
        for name, value in fields:
            if not isinstance(value, str) or '\r' in value or '\n' in value or len(value) > 512:
                return {'error': 'request'}
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.extend([
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
            audio.read_bytes(), f'\r\n--{boundary}--\r\n'.encode(),
        ])
        body = b''.join(parts)
        if len(body) > maximum:
            return {'error': 'request'}
        cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        connection = cls(parsed.hostname, port=parsed.port, timeout=connect_timeout)
        connection.connect()
        connection.sock.settimeout(read_timeout)
        headers = {'Content-Type': f'multipart/form-data; boundary={boundary}', 'Accept': 'application/json, text/plain'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        connection.request('POST', parsed.path or '/', body, headers)
        response = connection.getresponse()
        # Do not return remote error bodies: they can echo Authorization or URLs.
        if response.status != 200:
            return {'status': response.status}
        deadline = time.monotonic() + read_timeout
        chunks, count = [], 0
        while True:
            if time.monotonic() >= deadline:
                return {'error': 'timeout'}
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > MAX_RESPONSE_BYTES:
                return {'error': 'response'}
        return {'status': response.status, 'payload': b''.join(chunks).decode('utf-8-sig')}
    except (ValueError, TypeError, KeyError, UnicodeError):
        return {'error': 'request'}
    except (socket.timeout, TimeoutError):
        return {'error': 'timeout'}
    except ssl.SSLError:
        return {'error': 'tls'}
    except (OSError, http.client.HTTPException):
        return {'error': 'network'}
    finally:
        if connection is not None:
            connection.close()


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read(65537))
        result = request(data) if isinstance(data, dict) else {'error': 'request'}
    except Exception:
        # Owned helper never prints exception objects, request data or tracebacks.
        result = {'error': 'request'}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
