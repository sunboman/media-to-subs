import errno
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from media_to_subs.errors import CancelledError, FileSafetyError, ValidationError
from media_to_subs.files import atomic_publish, scratch_directory, validate_output_path
from media_to_subs.runtime import Cancellation
from media_to_subs.subtitles import parse_srt

SRT = "1\n00:00:00,000 --> 00:00:01,000\nSynthetic cue\n"


def valid_srt(data):
    parse_srt(data.decode("utf-8"))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.media = self.root / "sample.wav"
        self.media.write_bytes(b"synthetic media placeholder")
        self.sidecar = self.root / "sample.ja.srt"
        self.sidecar.write_text(SRT)
        self.output = self.root / "sample.zh.srt"

    def publish(self, **kwargs):
        return atomic_publish(self.output, SRT, input_path=self.media,
                              sidecars=(self.sidecar,), validator=valid_srt, **kwargs)

    def test_publish_preserves_input_sidecars_and_cleans_staging(self):
        media_before, sidecar_before = self.media.read_bytes(), self.sidecar.read_bytes()
        self.assertEqual(self.publish(), self.output)
        self.assertEqual(self.output.read_text(), SRT)
        self.assertEqual(self.media.read_bytes(), media_before)
        self.assertEqual(self.sidecar.read_bytes(), sidecar_before)
        self.assertEqual(set(self.root.iterdir()), {self.media, self.sidecar, self.output})

    def test_aliases_symlinks_and_nonregular_outputs_rejected(self):
        hardlink = self.root / "hardlink.srt"
        os.link(self.media, hardlink)
        symlink = self.root / "symlink.srt"
        symlink.symlink_to(self.sidecar)
        dangling = self.root / "dangling.srt"
        dangling.symlink_to(self.root / "nonexistent")
        for target in (self.media, self.sidecar, hardlink, symlink, dangling, self.root):
            with self.subTest(target=target), self.assertRaises(FileSafetyError):
                validate_output_path(target, input_path=self.media, sidecars=(self.sidecar,), overwrite=True)
        parent_alias = self.root / "alias-dir"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(FileSafetyError):
            validate_output_path(parent_alias / self.media.name, input_path=self.media, overwrite=True)

    def test_existing_output_requires_overwrite(self):
        self.output.write_text("old output")
        with self.assertRaises(FileSafetyError):
            self.publish()
        self.assertEqual(self.output.read_text(), "old output")
        self.publish(overwrite=True)
        self.assertEqual(self.output.read_text(), SRT)

    def test_no_clobber_when_competitor_creates_destination_at_commit(self):
        original_link = os.link
        ready, done = threading.Event(), threading.Event()

        def competitor():
            if ready.wait(3):
                self.output.write_text("competitor wins")
            done.set()

        def raced_link(*args, **kwargs):
            ready.set()
            self.assertTrue(done.wait(3))
            return original_link(*args, **kwargs)

        thread = threading.Thread(target=competitor)
        thread.start()
        try:
            with patch("media_to_subs.files.os.link", side_effect=raced_link):
                with self.assertRaises(FileSafetyError):
                    self.publish()
        finally:
            ready.set()
            thread.join(3)
        self.assertEqual(self.output.read_text(), "competitor wins")
        self.assertEqual(set(self.root.iterdir()), {self.media, self.sidecar, self.output})

    def test_failed_overwrite_retains_old_complete_file(self):
        self.output.write_text("old complete output")
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation), patch(f"media_to_subs.files.os.{operation}",
                                                          side_effect=OSError(errno.ENOSPC, "synthetic disk full")):
                with self.assertRaises(FileSafetyError):
                    self.publish(overwrite=True)
            self.assertEqual(self.output.read_text(), "old complete output")
            self.assertEqual(set(self.root.iterdir()), {self.media, self.sidecar, self.output})

    def test_validation_failure_retains_old_file(self):
        self.output.write_text("old complete output")
        with self.assertRaises(ValidationError):
            atomic_publish(self.output, b"invalid subtitle", input_path=self.media,
                           overwrite=True, validator=valid_srt)
        self.assertEqual(self.output.read_text(), "old complete output")
        self.assertEqual(set(self.root.iterdir()), {self.media, self.sidecar, self.output})

    def test_recheck_rejects_new_protected_hardlink_or_symlink(self):
        for link in (lambda: os.link(self.media, self.output), lambda: self.output.symlink_to(self.sidecar)):
            def validate_and_race(data):
                valid_srt(data)
                link()
            with self.assertRaises(FileSafetyError):
                atomic_publish(self.output, SRT, input_path=self.media, sidecars=(self.sidecar,),
                               overwrite=True, validator=validate_and_race)
            self.output.unlink()
        self.assertEqual(self.media.read_bytes(), b"synthetic media placeholder")
        self.assertEqual(self.sidecar.read_text(), SRT)

    def test_parent_replacement_does_not_redirect_commit_or_cleanup(self):
        directory = self.root / "destination"
        directory.mkdir()
        moved = self.root / "moved"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        def validate_and_swap(data):
            valid_srt(data)
            directory.rename(moved)
            directory.symlink_to(elsewhere, target_is_directory=True)

        with self.assertRaises(FileSafetyError):
            atomic_publish(directory / "out.srt", SRT, input_path=self.media,
                           validator=validate_and_swap)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    def test_cancellation_before_commit_preserves_old_file(self):
        self.output.write_text("old output")
        cancellation = Cancellation()

        def validate_and_cancel(data):
            valid_srt(data)
            cancellation.cancel()

        with self.assertRaises(CancelledError):
            atomic_publish(self.output, SRT, input_path=self.media, validator=validate_and_cancel,
                           overwrite=True, cancellation=cancellation)
        self.assertEqual(self.output.read_text(), "old output")

    def test_scratch_cleanup_only_removes_owned_tree_without_following_symlinks(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        keep = unrelated / "keep"
        keep.write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with scratch_directory(self.root) as scratch:
                (scratch / "owned").write_text("delete")
                (scratch / "external").symlink_to(unrelated, target_is_directory=True)
                self.assertNotEqual(scratch, self.root)
                raise RuntimeError("synthetic failure")
        self.assertFalse(scratch.exists())
        self.assertEqual(keep.read_text(), "keep")

    def test_scratch_refuses_cleanup_if_its_path_has_been_replaced(self):
        with self.assertRaises(FileSafetyError):
            with scratch_directory(self.root) as scratch:
                scratch.rename(self.root / "moved-scratch")
                scratch.mkdir()
                (scratch / "not-ours").write_text("keep")
        self.assertEqual((scratch / "not-ours").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
