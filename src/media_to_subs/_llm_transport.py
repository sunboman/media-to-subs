"""Private cancellable HTTP bridge shared by translation and server readiness."""

import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

from .config import validate_url
from .errors import ProcessingError, ProcessTimeoutError, ProviderError
from .runtime import Cancellation, run_process


class InvalidResponseError(ProviderError):
    """HTTP succeeded but JSON could not be decoded."""


def endpoint_url(url: str, *, local: bool) -> str:
    validate_url(url, local=local)
    parts = urlsplit(url.rstrip("/"))
    if local and parts.hostname.lower() == "localhost":
        # Pin localhost to a literal address; do not trust resolver overrides.
        netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))
    return url.rstrip("/")


def private_environment() -> dict[str, str]:
    """Explicit child environment; do not inherit provider/server configuration."""
    return {key: os.environ[key] for key in ("PATH", "SystemRoot", "TMPDIR", "LANG", "LC_ALL")
            if key in os.environ}


def request_json(url: str, *, method: str, payload: dict | None, api_key: str | None,
                 connect_timeout: float, read_timeout: float, cancellation: Cancellation,
                 timeout: float | None = None) -> tuple[int, object]:
    cancellation.raise_if_cancelled()
    request = {"url": url, "method": method, "payload": payload, "api_key": api_key,
               "connect_timeout": connect_timeout, "read_timeout": read_timeout}
    budget = connect_timeout + read_timeout if timeout is None else timeout
    try:
        result = run_process([sys.executable, "-I", "-m", "media_to_subs._llm_http"],
                             timeout=budget, cancellation=cancellation, env=private_environment(),
                             input_text=json.dumps(request, ensure_ascii=True), check=False)
    except ProcessTimeoutError:
        raise ProviderError("LLM request exceeded its wall-clock timeout; remote work may have continued") from None
    except ProcessingError:
        raise ProviderError("Could not run LLM HTTP transport") from None
    if result.returncode != 0:
        raise ProviderError("LLM HTTP transport failed")
    try:
        envelope = json.loads(result.stdout)
    except (ValueError, RecursionError):
        raise ProviderError("LLM HTTP transport returned an invalid result") from None
    if not isinstance(envelope, dict):
        raise ProviderError("LLM HTTP transport returned an invalid result")
    error = envelope.get("error")
    if error == "invalid_json":
        raise InvalidResponseError("LLM response is not valid JSON")
    if error == "too_large":
        raise ProviderError("LLM response exceeded the 2 MiB limit")
    if error == "timeout":
        raise ProviderError("LLM request timed out; remote work may have continued")
    if error is not None:
        raise ProviderError("LLM request failed; remote work may have continued")
    status = envelope.get("status")
    if type(status) is not int or not 100 <= status <= 599:
        raise ProviderError("LLM HTTP transport returned an invalid status")
    return status, envelope.get("data")
