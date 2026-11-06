"""Private requests subprocess: JSON on stdin/stdout, no secrets in argv/errors.

A process boundary lets the caller interrupt DNS, stalled headers/body reads, and
slow trickle responses without leaving an in-flight background thread behind.
"""

import json
import sys

import requests

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def exchange(request: dict) -> dict:
    headers = {"Accept": "application/json"}
    if request.get("api_key"):
        headers["Authorization"] = "Bearer " + request["api_key"]
    with requests.Session() as session:
        session.trust_env = False  # No proxies, netrc or implicit CA environment.
        try:
            with session.request(request["method"], request["url"], headers=headers,
                                 json=request.get("payload"), allow_redirects=False,
                                 timeout=(request["connect_timeout"], request["read_timeout"]),
                                 stream=True) as response:
                if response.status_code != 200:
                    # Never read/forward provider error bodies or redirect targets.
                    return {"status": response.status_code}
                body = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        return {"error": "too_large"}
                try:
                    return {"status": 200, "data": json.loads(body)}
                except (ValueError, UnicodeError, RecursionError):
                    return {"error": "invalid_json"}
        except requests.Timeout:
            return {"error": "timeout"}
        except (requests.RequestException, ValueError, UnicodeError, OSError):
            return {"error": "network"}


def main() -> None:
    try:
        request = json.load(sys.stdin)
        result = exchange(request)
    except Exception:
        # This helper never exposes a traceback containing a URL or header.
        result = {"error": "transport"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
