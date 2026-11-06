"""Cancellation and bounded execution of processes owned by this invocation."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import signal
import selectors
import subprocess
import threading
import time
from typing import IO

from .errors import CancelledError, ConfigurationError, ProcessingError, ProcessTimeoutError


class Cancellation:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError("Operation cancelled")

    def wait(self, timeout: float | None = None) -> bool:
        """Interruptible delay for adapter retry/readiness loops; True if cancelled."""
        return self._event.wait(timeout)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


def _signal_owned(process: subprocess.Popen, sig: int) -> bool:
    try:
        if os.name == "posix":
            # start_new_session makes this process the leader of its own group.
            os.killpg(process.pid, sig)
        elif process.poll() is None:
            process.terminate() if sig == signal.SIGTERM else process.kill()
        else:
            return False
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can report EPERM for a group containing only exited/zombie
        # processes. A reaped leader plus no signalable group is already stopped.
        if process.poll() is not None:
            return False
        raise ProcessingError("Could not signal owned process group") from None


def _stop_owned(process: subprocess.Popen) -> None:
    # Kill the owned group even if its leader exited while descendants kept pipes
    # open. Never discover/kill processes by executable name or occupied port.
    try:
        if _signal_owned(process, signal.SIGTERM):
            time.sleep(0.2)
            _signal_owned(process, signal.SIGKILL)
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=0.5)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BrokenPipeError:
                    pass


def _command(args: Sequence[str | os.PathLike[str]]) -> list[str]:
    if isinstance(args, (str, bytes)) or not isinstance(args, Sequence) or not args:
        raise ConfigurationError("Process arguments must be a nonempty sequence")
    try:
        command = [os.fspath(arg) for arg in args]
    except TypeError:
        raise ConfigurationError("Process arguments must be strings or paths") from None
    if any(not isinstance(arg, str) or "\x00" in arg for arg in command) or not command[0]:
        raise ConfigurationError("Invalid process arguments")
    return command


@contextmanager
def owned_process(args: Sequence[str | os.PathLike[str]], *, cancellation: Cancellation,
                  cwd: Path | None = None, env: Mapping[str, str] | None = None,
                  stdout: IO[bytes] | int = subprocess.DEVNULL,
                  stderr: IO[bytes] | int = subprocess.DEVNULL) -> Iterator[subprocess.Popen[bytes]]:
    """Own a server's lifetime; caller polls readiness, deadline and cancellation.

    Streams must be DEVNULL, files, or stderr=STDOUT, not unconsumed PIPEs.
    The yielded public Popen supports poll/wait; context exit stops its group.
    """
    command = _command(args)
    if stdout == subprocess.PIPE or stderr == subprocess.PIPE:
        raise ConfigurationError("Use files for server logs or run_process for captured pipes")
    cancellation.raise_if_cancelled()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   cwd=cwd, env=env, shell=False, start_new_session=os.name == "posix")
    except (OSError, ValueError):
        raise ProcessingError("Could not start owned process; check executable and working directory") from None
    try:
        cancellation.raise_if_cancelled()
        yield process
    finally:
        _stop_owned(process)


def run_process(args: Sequence[str | os.PathLike[str]], *, timeout: float,
                cancellation: Cancellation, cwd: Path | None = None,
                env: Mapping[str, str] | None = None, check: bool = True,
                input_text: str | bytes | None = None,
                max_output_bytes: int | None = None) -> ProcessResult:
    """Run without a shell, capturing UTF-8 output (invalid bytes replaced).

    timeout is a monotonic total execution budget in seconds. Cleanup may take up
    to 1.2 extra seconds. POSIX children inherit an owned process group; programs
    must not daemonize/escape it. No signal handlers are installed here.
    """
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ConfigurationError("Process timeout must be finite and positive")
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes <= 0):
        raise ConfigurationError("Process output limit must be a positive integer")
    if max_output_bytes is not None and os.name != "posix":
        raise ConfigurationError("Bounded process capture currently requires POSIX")
    command = _command(args)
    if input_text is not None and not isinstance(input_text, (str, bytes)):
        raise ConfigurationError("Process stdin must be text or bytes")
    payload = input_text.encode("utf-8") if isinstance(input_text, str) else input_text
    cancellation.raise_if_cancelled()
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, env=env, shell=False,
                                   start_new_session=os.name == "posix")
    except (OSError, ValueError):
        raise ProcessingError("Could not start process; check executable and working directory") from None
    try:
        if max_output_bytes is not None:
            stdout, stderr = _bounded_capture(process, payload, deadline, cancellation, max_output_bytes)
        else:
            while True:
                cancellation.raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessTimeoutError("Process exceeded its execution timeout")
                try:
                    stdout, stderr = process.communicate(input=payload, timeout=min(remaining, 0.05))
                    break
                except subprocess.TimeoutExpired:
                    # communicate retains the unsent bytes and offset internally.
                    payload = None
                    continue
        cancellation.raise_if_cancelled()
        if check and process.returncode != 0:
            raise ProcessingError(f"Process failed with exit code {process.returncode}")
        return ProcessResult(process.returncode, stdout.decode("utf-8", errors="replace"),
                             stderr.decode("utf-8", errors="replace"))
    finally:
        _stop_owned(process)


def _bounded_capture(process, payload, deadline, cancellation, limit):
    """Drain both pipes while enforcing a combined byte cap before retaining data.

    Nonblocking stdin avoids deadlock when the child writes before reading its
    prompt. Selectors keep cancellation/deadlines active even with inherited
    pipes held open by descendants after the leader exits.
    """
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    offset = total = 0
    with selectors.DefaultSelector() as selector:
        for stream in buffers:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        if process.stdin is not None:
            if payload:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
        while selector.get_map() or process.poll() is None:
            cancellation.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessTimeoutError("Process exceeded its execution timeout")
            for key, _ in selector.select(min(remaining, 0.05)):
                stream = key.fileobj
                try:
                    if stream is process.stdin:
                        try:
                            offset += os.write(stream.fileno(), payload[offset:offset + 4096])
                        except BrokenPipeError:
                            offset = len(payload)
                        if offset == len(payload):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(stream.fileno(), min(65536, limit - total + 1))
                        if not chunk:
                            selector.unregister(stream)
                        else:
                            total += len(chunk)
                            if total > limit:
                                raise ProcessingError("Process exceeded its output limit")
                            buffers[stream].extend(chunk)
                except BlockingIOError:
                    continue
    process.wait()
    return bytes(buffers[process.stdout]), bytes(buffers[process.stderr])
