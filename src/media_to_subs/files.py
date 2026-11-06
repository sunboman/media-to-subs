"""Per-run scratch ownership and validated, atomic output publication."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from .errors import FileSafetyError
from .runtime import Cancellation


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _protected_paths(paths: Iterable[Path]) -> tuple[set[Path], set[tuple[int, int]]]:
    resolved: set[Path] = set()
    identities: set[tuple[int, int]] = set()
    for path in paths:
        path = Path(path)
        resolved.add(path.resolve())
        try:
            identities.add(_identity(path.stat()))
        except FileNotFoundError:
            pass
    return resolved, identities


def _destination(output: Path) -> Path:
    requested = Path(output).absolute()
    if requested.is_symlink():
        raise FileSafetyError("Output must not be a symlink")
    parent = requested.parent.resolve(strict=True)
    if not parent.is_dir() or not requested.name:
        raise FileSafetyError("Output requires an existing parent directory and filename")
    return parent / requested.name


def _check_destination(destination: Path, protected: tuple[set[Path], set[tuple[int, int]]],
                       *, overwrite: bool, directory_fd: int | None = None) -> None:
    paths, identities = protected
    if destination in paths:
        raise FileSafetyError("Output aliases the input or a protected sidecar")
    try:
        info = os.stat(destination if directory_fd is None else destination.name,
                       dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise FileSafetyError("Existing output must be a regular file, not a symlink")
    if _identity(info) in identities:
        raise FileSafetyError("Output aliases the input or a protected sidecar")
    if not overwrite:
        raise FileSafetyError("Output already exists; use overwrite explicitly")


def validate_output_path(output: Path, *, input_path: Path,
                         sidecars: Iterable[Path] = (), overwrite: bool = False) -> Path:
    """Preflight only; atomic_publish repeats checks at publication time."""
    try:
        destination = _destination(output)
        protected = _protected_paths((input_path, *sidecars))
        _check_destination(destination, protected, overwrite=overwrite)
        return destination
    except (OSError, RuntimeError, ValueError):
        raise FileSafetyError("Cannot validate output destination and protected files") from None


def _open_directory(path: Path) -> int:
    # Walk the canonical path without following a component swapped to a symlink.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def atomic_publish(output: Path, data: str | bytes, *, input_path: Path,
                   validator: Callable[[bytes], None], sidecars: Iterable[Path] = (),
                   overwrite: bool = False, cancellation: Cancellation | None = None) -> Path:
    """Validate, fsync a private sibling, and atomically link/replace the output.

    Uses POSIX directory descriptors. Final-name symlinks are never followed.
    No-clobber is guaranteed by link(), not an existence check. Replacing a final
    symlink raced in after the last check unlinks that entry, never its target.
    """
    if not isinstance(data, (str, bytes)) or not callable(validator):
        raise FileSafetyError("Publication requires text/bytes and a validator")
    try:
        payload = data.encode("utf-8") if isinstance(data, str) else data
    except UnicodeError:
        raise FileSafetyError("Output text must be valid UTF-8") from None
    cancellation = cancellation if cancellation is not None else Cancellation()
    cancellation.raise_if_cancelled()
    sidecars = tuple(sidecars)
    destination = validate_output_path(output, input_path=input_path, sidecars=sidecars, overwrite=overwrite)
    directory_fd: int | None = None
    temporary: str | None = None
    committed = False
    try:
        protected = _protected_paths((input_path, *sidecars))
        directory_fd = _open_directory(destination.parent)
        directory_identity = _identity(os.fstat(directory_fd))
        _check_destination(destination, protected, overwrite=overwrite, directory_fd=directory_fd)
        name = f".media-to-subs-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        temporary = name
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        validator(payload)
        cancellation.raise_if_cancelled()
        # Anchoring all operations to an fd prevents parent symlink swaps from
        # redirecting writes or cleanup. Refuse a changed public path as well.
        if (destination.parent.is_symlink()
                or _identity(destination.parent.stat()) != directory_identity
                or _destination(output) != destination):
            raise FileSafetyError("Output directory changed during publication")
        fresh_paths, fresh_ids = _protected_paths((input_path, *sidecars))
        protected[0].update(fresh_paths)
        protected[1].update(fresh_ids)
        _check_destination(destination, protected, overwrite=overwrite, directory_fd=directory_fd)
        if overwrite:
            os.replace(temporary, destination.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temporary = None
        else:
            os.link(temporary, destination.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False)
        committed = True
        return destination
    except OSError:
        raise FileSafetyError("Cannot safely publish output; check destination, permissions and disk space") from None
    finally:
        if directory_fd is not None:
            try:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        message = ("Output published, but private staging cleanup failed" if committed
                                   else "Publication failed and private staging cleanup failed")
                        raise FileSafetyError(message) from None
            finally:
                os.close(directory_fd)


@contextmanager
def scratch_directory(parent: Path | None = None) -> Iterator[Path]:
    """Create one private run directory; clean only that directory's identity."""
    try:
        directory = Path(tempfile.mkdtemp(prefix="media-to-subs-", dir=parent)).resolve()
        identity = _identity(directory.stat())
    except OSError:
        raise FileSafetyError("Cannot create per-run scratch directory") from None
    try:
        yield directory
    finally:
        try:
            current = directory.lstat()
        except FileNotFoundError:
            current = None
        if current is not None:
            if not stat.S_ISDIR(current.st_mode) or _identity(current) != identity:
                raise FileSafetyError("Scratch path was replaced; refusing to clean unowned files")
            try:
                shutil.rmtree(directory)
            except OSError:
                raise FileSafetyError("Cannot clean owned scratch directory") from None
