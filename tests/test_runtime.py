import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from media_to_subs.errors import CancelledError, ProcessingError, ProcessTimeoutError
from media_to_subs.runtime import Cancellation, owned_process, run_process


class RuntimeTests(unittest.TestCase):
    def test_output_limit_stops_flooding_process_and_bounds_both_streams(self):
        for target in ('stdout', 'stderr'):
            with tempfile.TemporaryDirectory() as directory:
                pidfile = Path(directory) / 'pid'
                script = ("import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                          f"stream=sys.{target}.buffer\nwhile True: stream.write(b'x'*65536); stream.flush()")
                with self.assertRaisesRegex(ProcessingError, 'output limit'):
                    run_process([sys.executable, '-c', script, str(pidfile)], timeout=5,
                                cancellation=Cancellation(), max_output_bytes=10000)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)

    def test_bounded_capture_transports_large_stdin_and_counts_combined_bytes(self):
        result = run_process([sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'],
                             input_text='x' * 500000, timeout=5, cancellation=Cancellation(), max_output_bytes=100)
        self.assertEqual(result.stdout, '500000\n')
        with self.assertRaisesRegex(ProcessingError, 'output limit'):
            run_process([sys.executable, '-c', "import os; os.write(1,b'x'*60); os.write(2,b'y'*60)"],
                        timeout=5, cancellation=Cancellation(), max_output_bytes=100)

    def test_owned_server_context_cleans_up_on_cancellation_and_normal_exit(self):
        for cancel in (False, True):
            cancellation = Cancellation()
            pid = None
            try:
                with owned_process([sys.executable, "-c", "import time; time.sleep(20)"],
                                   cancellation=cancellation) as process:
                    pid = process.pid
                    self.assertIsNone(process.poll())
                    if cancel:
                        cancellation.cancel()
                        cancellation.raise_if_cancelled()
            except CancelledError:
                self.assertTrue(cancel)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_owned_server_can_log_without_blocking_on_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            logfile = Path(directory) / "log"
            with logfile.open("wb") as log:
                with owned_process([sys.executable, "-c", "print('x'*200000)"],
                                   cancellation=Cancellation(), stdout=log, stderr=subprocess.STDOUT) as process:
                    process.wait(timeout=5)
            self.assertEqual(logfile.stat().st_size, 200001)

    def test_capture_is_complete_without_shell_interpolation(self):
        literal = "$(echo DO-NOT-EXECUTE); literal * argument"
        result = run_process([sys.executable, "-c", "import sys; print(sys.argv[1]); print('err', file=sys.stderr)", literal],
                             timeout=5, cancellation=Cancellation())
        self.assertEqual(result.stdout, literal + "\n")
        self.assertEqual(result.stderr, "err\n")
        self.assertEqual(result.returncode, 0)

    def test_large_both_streams_do_not_deadlock(self):
        result = run_process([sys.executable, "-c", "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)"],
                             timeout=5, cancellation=Cancellation())
        self.assertEqual((len(result.stdout), len(result.stderr)), (200000, 200000))

    def test_stdin_accepts_text_and_bytes_without_putting_payload_in_arguments(self):
        for payload in ('{"key":"synthetic-private-例"}', b"binary\x00payload"):
            expected = payload.encode() if isinstance(payload, str) else payload
            result = run_process([sys.executable, "-c", "import sys; data=sys.stdin.buffer.read(); print(data.hex()); print(len(sys.argv))"],
                                 input_text=payload, timeout=5, cancellation=Cancellation())
            self.assertEqual(result.stdout.splitlines(), [expected.hex(), "1"])

    def test_cancel_while_child_slowly_consumes_stdin(self):
        cancellation = Cancellation()
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            child = "import pathlib,sys,time; sys.stdin.buffer.read(1); pathlib.Path(sys.argv[1]).touch();\nwhile sys.stdin.buffer.read(1): time.sleep(.01)"
            stopped = threading.Event()

            def cancel_when_reading():
                while not stopped.wait(.01):
                    if ready.exists():
                        cancellation.cancel()
                        return

            watcher = threading.Thread(target=cancel_when_reading)
            watcher.start()
            started = time.monotonic()
            try:
                with self.assertRaises(CancelledError):
                    run_process([sys.executable, "-c", child, str(ready)], input_text="x" * 500000,
                                timeout=5, cancellation=cancellation)
                self.assertLess(time.monotonic() - started, 3)
            finally:
                stopped.set()
                watcher.join(1)

    def test_failures_do_not_echo_commands_or_process_output(self):
        command = [sys.executable, "-c", "import sys; print('private-marker', file=sys.stderr); sys.exit(7)"]
        with self.assertRaises(ProcessingError) as raised:
            run_process(command, timeout=5, cancellation=Cancellation())
        self.assertNotIn("private-marker", str(raised.exception))
        result = run_process(command, timeout=5, cancellation=Cancellation(), check=False)
        self.assertEqual(result.returncode, 7)
        self.assertIn("private-marker", result.stderr)
        self.assertNotIn("private-marker", repr(result))
        with self.assertRaises(ProcessingError):
            run_process(["/nonexistent/synthetic-binary"], timeout=5, cancellation=Cancellation())

    def test_pre_cancel_does_not_launch_process(self):
        cancellation = Cancellation()
        cancellation.cancel()
        self.assertTrue(cancellation.cancelled)
        self.assertTrue(cancellation.wait(0))
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            with self.assertRaises(CancelledError):
                run_process([sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)],
                            timeout=5, cancellation=cancellation)
            self.assertFalse(marker.exists())

    def test_timeout_is_bounded_and_reaps_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            code = "import os,pathlib,signal,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)"
            start = time.monotonic()
            with self.assertRaises(ProcessTimeoutError):
                run_process([sys.executable, "-c", code, str(pidfile)], timeout=0.6, cancellation=Cancellation())
            self.assertLess(time.monotonic() - start, 3)
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @unittest.skipUnless(os.name == "posix", "Owned process-group verification requires POSIX")
    def test_cancellation_terminates_descendants_but_keeps_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat"
            child = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);\nwhile True:\n p.write_text(str(time.monotonic_ns())); time.sleep(.02)"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(20)"
            cancellation = Cancellation()
            stopped = threading.Event()

            def cancel_when_ready():
                deadline = time.monotonic() + 5
                while not stopped.wait(0.01) and time.monotonic() < deadline:
                    if heartbeat.exists():
                        cancellation.cancel()
                        return

            watcher = threading.Thread(target=cancel_when_ready)
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
            watcher.start()
            try:
                with self.assertRaises(CancelledError):
                    run_process([sys.executable, "-c", parent, child, str(heartbeat)],
                                timeout=6, cancellation=cancellation, max_output_bytes=10000)
                self.assertIsNone(unrelated.poll())
                before = heartbeat.read_text()
                time.sleep(0.15)
                self.assertEqual(heartbeat.read_text(), before)
            finally:
                stopped.set()
                watcher.join(1)
                unrelated.terminate()
                unrelated.wait(timeout=2)

    def test_explicit_working_directory_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_process([sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ['SYNTHETIC'])"],
                                 cwd=Path(directory), env={"SYNTHETIC": "yes"},
                                 timeout=5, cancellation=Cancellation())
            self.assertEqual(result.stdout.splitlines(), [str(Path(directory).resolve()), "yes"])


if __name__ == "__main__":
    unittest.main()
