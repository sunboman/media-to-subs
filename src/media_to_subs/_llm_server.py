"""Optional local llama-server lifecycle; never owns an existing listener."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
import shutil
import socket
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from ._llm_transport import endpoint_url, private_environment, request_json
from .config import LLMConfig
from .errors import ConfigurationError, ProviderError
from .runtime import Cancellation, owned_process
from .types import Progress


def _check_port(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError:
        raise ProviderError("Managed LLM port is unavailable; existing listeners are never stopped") from None


@contextmanager
def server_config(config: LLMConfig, cancellation: Cancellation,
                  progress: Progress) -> Iterator[tuple[LLMConfig, Callable[[], None]]]:
    """Yield a route plus a liveness check; owned_process supplies group cleanup."""
    cancellation.raise_if_cancelled()
    if not config.managed_server:
        yield config, cancellation.raise_if_cancelled
        return
    base = endpoint_url(config.url or "http://127.0.0.1:8080/v1", local=True)
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.path not in ("/v1", "/v1/chat/completions"):
        raise ConfigurationError("Managed llama-server requires an HTTP loopback /v1 endpoint")
    try:
        has_model = config.model_path is not None and config.model_path.is_file()
    except OSError:
        has_model = False
    if not has_model:
        raise ConfigurationError("Managed llama-server requires an existing local model file")
    binary = shutil.which(config.server_binary)
    if binary is None:
        raise ConfigurationError("Managed llama-server executable is unavailable")
    host, port = parsed.hostname, parsed.port or 80
    _check_port(host, port)
    # llama-server cannot inherit our bound socket. A unique per-launch model
    # alias detects a different server winning the bind race before any POST.
    alias = "media-to-subs-" + uuid4().hex
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    args = [binary, "--model", str(config.model_path), "--host", host, "--port", str(port),
            "--alias", alias, "--ctx-size", "2048", "--threads", str(config.cpu_threads),
            "--gpu-layers", "0", "--parallel", "1", "--offline", "--no-webui"]
    environment = private_environment()
    if config.api_key is not None:
        environment["LLAMA_API_KEY"] = config.api_key
    deadline = time.monotonic() + config.startup_timeout
    progress("Starting managed local LLM")
    with owned_process(args, cancellation=cancellation, env=environment) as process:
        def check_alive() -> None:
            cancellation.raise_if_cancelled()
            if process.poll() is not None:
                raise ProviderError("Managed llama-server exited before translation completed")

        def probe(path: str) -> tuple[int, object]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Managed llama-server startup timed out")
            return request_json(origin + path, method="GET", payload=None, api_key=config.api_key,
                                connect_timeout=min(config.connect_timeout, 0.25),
                                read_timeout=min(config.read_timeout, 0.5), cancellation=cancellation,
                                # The subprocess budget includes Python/import
                                # startup as well as the short socket timeouts.
                                timeout=remaining)

        while True:
            check_alive()
            if time.monotonic() >= deadline:
                raise ProviderError("Managed llama-server startup timed out")
            try:
                status, health = probe("/health")
            except ProviderError:
                # Connection refusal while a model loads is expected. Cancellation
                # is a separate error and always escapes this readiness loop.
                cancellation.wait(min(0.1, max(0, deadline - time.monotonic())))
                continue
            check_alive()
            if status == 200 and isinstance(health, dict) and health.get("status") == "ok":
                try:
                    status, models = probe("/v1/models")
                except ProviderError:
                    # Readiness GETs are safe to retry while the same owned
                    # process loads or a probe exceeds its short budget.
                    cancellation.wait(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                check_alive()
                items = models.get("data") if isinstance(models, dict) else None
                if (status != 200 or not isinstance(items, list)
                        or not any(isinstance(item, dict) and item.get("id") == alias for item in items)):
                    raise ProviderError("Managed llama-server readiness identity did not match the owned process")
                break
            if status not in (200, 503):
                raise ProviderError("Managed llama-server readiness request was rejected")
            cancellation.wait(min(0.1, max(0, deadline - time.monotonic())))
        check_alive()
        progress("Managed local LLM ready")
        yield replace(config, model=alias, url=origin + "/v1"), check_alive
