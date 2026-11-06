#!/usr/bin/env python3
"""Install a local checkout or wheel into an owned, immutable user environment.

The command symlink is the commit point. Environments are created at their final
paths, verified before publication, and retained across upgrades. Advisory locks
serialize installers; private directories and strict receipts bound all deletion.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

APP = "media-to-subs"
ROOT_MARKER = ".installer.json"
ENV_MARKER = ".install.json"
PREREQUISITES = (
    "Python 3.11+ with venv/ensurepip is required. macOS: brew install python. "
    "Debian/Ubuntu: install python3-venv for your Python 3.11+ interpreter. "
    "Then rerun ./install.sh (or set MEDIA_TO_SUBS_PYTHON to that executable)."
)


class InstallError(Exception):
    """An actionable failure without subprocess output or credentials."""


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise InstallError("Installation directories must be absolute paths without '..'.")
    return path


def directory(path, *, private=False, create=False):
    """Inspect every ancestor before creating or using an owned directory."""
    path = absolute_path(path)
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if not create:
                raise InstallError("An installation directory is missing.") from None
            component.mkdir(mode=0o700)
            info = component.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError("Installation directories must not be symlinks or special files.")
        if info.st_uid not in (0, os.getuid()):
            raise InstallError("Installation directory has an unexpected owner.")
        # A system-owned sticky temporary ancestor is safe for isolated tests.
        sticky_system = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if info.st_mode & 0o022 and not sticky_system:
            raise InstallError("Installation directories must not be writable by other users.")
    info = path.lstat()
    if info.st_uid != os.getuid() or (private and info.st_mode & 0o077):
        raise InstallError("Installer-owned directories require your ownership and mode 0700.")
    return path


@contextmanager
def owned_file(path, flags=os.O_RDONLY):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise InstallError("Invalid installer file ownership, links or permissions.")
        with os.fdopen(fd, "rb" if flags == os.O_RDONLY else "r+b") as stream:
            fd = None
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def lock(path):
    with owned_file(path, os.O_RDWR | os.O_CREAT) as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("Another installer is running; retry after it exits.") from None
        yield


def read_record(path):
    try:
        with owned_file(path) as stream:
            result = json.load(stream)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (OSError, ValueError):
        raise InstallError("Missing or invalid installation ownership record; files preserved.") from None


def write_record(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with owned_file(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL) as stream:
            stream.write((json.dumps(value, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def root_record(root, bin_dir):
    return {"schema": 1, "app": APP, "uid": os.getuid(),
            "root": str(root), "bin_dir": str(bin_dir)}


def prepare_root(root, bin_dir):
    directory(root, private=True, create=True)
    marker = root / ROOT_MARKER
    expected = root_record(root, bin_dir)
    if not os.path.lexists(marker):
        if any(root.iterdir()):
            raise InstallError("Refusing to claim a nonempty installation directory without ownership records.")
        write_record(marker, expected)
    if read_record(marker) != expected:
        raise InstallError("Installation ownership or bin directory differs; files preserved.")
    directory(root / "installs", private=True, create=True)


def environment_record(environment, root, bin_dir, *, ready=False, version=None):
    return root_record(root, bin_dir) | {
        "environment": str(environment), "ready": ready, "version": version,
    }


def validate_environment_owner(environment, root, bin_dir, *, ready=False):
    if (environment.parent != root / "installs"
            or not re.fullmatch(r"install-[a-f0-9]{32}", environment.name)):
        raise InstallError("Refusing an environment outside this installer's ownership.")
    directory(environment, private=True)
    record = read_record(environment / ENV_MARKER)
    expected = environment_record(environment, root, bin_dir,
                                  ready=record.get("ready"), version=record.get("version"))
    if (record != expected or type(record.get("ready")) is not bool
            or (ready and not record["ready"])
            or (record["ready"] and not isinstance(record.get("version"), str))):
        raise InstallError("Invalid environment ownership record; files preserved.")


def command_state(command, root, bin_dir):
    if not os.path.lexists(command):
        return None
    info = command.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise InstallError("An unrelated media-to-subs command already exists; choose another --bin-dir.")
    target = Path(os.readlink(command))
    if target.name != APP or target.parent.name != "bin":
        raise InstallError("Existing command is not owned by this installer.")
    validate_environment_owner(target.parent.parent, root, bin_dir, ready=True)
    directory(target.parent)
    target_info = target.lstat()
    if (not stat.S_ISREG(target_info.st_mode) or target_info.st_uid != os.getuid()
            or target_info.st_nlink != 1 or target_info.st_mode & 0o022):
        raise InstallError("Installed command has unsafe ownership or links.")
    return info.st_dev, info.st_ino, str(target)


def subprocess_environment():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "PIP_", "MEDIA_TO_SUBS_"))
           and key != "VIRTUAL_ENV"}
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


def run(argv, *, cwd, message, timeout=900):
    try:
        result = subprocess.run([str(arg) for arg in argv], cwd=cwd,
                                env=subprocess_environment(), capture_output=True,
                                text=True, timeout=timeout, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # pip output can contain credentials from network configuration. Never echo it.
        raise InstallError(message) from None


def build_environment(environment, source, *, wheelhouse=None, offline=False):
    run([sys.executable, "-I", "-m", "venv", environment], cwd=environment,
        message=PREREQUISITES)
    python = environment / "bin/python"
    wheels = environment / ".wheels"
    wheels.mkdir()
    pip = [python, "-I", "-m", "pip", "--disable-pip-version-check", "--no-input"]
    options = (["--no-index"] if offline else [])
    if wheelhouse is not None:
        options += ["--find-links", wheelhouse]
    run([*pip, "wheel", "--no-cache-dir", "--wheel-dir", wheels, *options, source],
        cwd=environment, message="Package build failed. Check internet access (or --wheelhouse with build dependencies), Python compatibility and free space; prior command preserved.")
    candidates = list(wheels.glob("media_to_subs-*.whl"))
    if len(candidates) != 1:
        raise InstallError("The local source did not build exactly one media-to-subs wheel.")
    run([*pip, "install", "--no-index", "--no-cache-dir", "--find-links", wheels, candidates[0]],
        cwd=environment, message="Package installation failed; check available disk space and Python compatibility. Prior command preserved.")
    shutil.rmtree(wheels)


def verify_environment(environment):
    python = environment / "bin/python"
    version = run([python, "-I", "-c",
                   "from importlib.metadata import version; "
                   "from media_to_subs.model_setup import DEFAULT_MANIFEST, load_manifest; "
                   "assert load_manifest(DEFAULT_MANIFEST); print(version('media-to-subs'))"],
                  cwd=environment, message="Installed package/resources failed validation.", timeout=30)
    command = environment / "bin" / APP
    help_text = run([command, "--help"], cwd=environment,
                    message="Installed command --help failed validation.", timeout=30)
    version_text = run([command, "--version"], cwd=environment,
                       message="Installed command --version failed validation.", timeout=30)
    if not version or f"{APP} {version}" not in version_text or "usage:" not in help_text.lower():
        raise InstallError("Installed command returned unexpected help/version output.")
    return version


def publish(command, target, previous, root, bin_dir):
    temporary = command.with_name(".media-to-subs." + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        if command_state(command, root, bin_dir) != previous:
            raise InstallError("Command changed during installation; existing command preserved.")
        if previous is None:
            # No-clobber creation also handles a command appearing after the check.
            os.link(temporary, command, follow_symlinks=False)
        else:
            os.replace(temporary, command)
    finally:
        temporary.unlink(missing_ok=True)


def remove_environment(environment, root, bin_dir):
    validate_environment_owner(environment, root, bin_dir)
    # CPython's fd-based rmtree does not follow substituted directory symlinks.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise InstallError("This Python cannot safely remove environments; files preserved.")
    shutil.rmtree(environment)


def install(source, data_home, bin_dir, *, wheelhouse=None, offline=False):
    app_data = absolute_path(data_home) / APP
    root = app_data / "app"
    bin_dir = absolute_path(bin_dir)
    environment = None
    committed = False
    try:
        source = Path(source).expanduser().resolve(strict=True)
        if not ((source.is_dir() and (source / "pyproject.toml").is_file())
                or (source.is_file() and (source.suffix == ".whl" or source.name.endswith(".tar.gz")))):
            raise InstallError("--source must be a local checkout, wheel or source archive.")
        directory(bin_dir, create=True)
        with lock(bin_dir / ".media-to-subs.install.lock"):
            directory(app_data, create=True)
            with lock(app_data / ".installer.lock"):
                prepare_root(root, bin_dir)
                previous = command_state(bin_dir / APP, root, bin_dir)
                environment = root / "installs" / ("install-" + uuid.uuid4().hex)
                environment.mkdir(mode=0o700)
                write_record(environment / ENV_MARKER, environment_record(environment, root, bin_dir))
                try:
                    build_environment(environment, source, wheelhouse=wheelhouse, offline=offline)
                    version = verify_environment(environment)
                    write_record(environment / ENV_MARKER,
                                 environment_record(environment, root, bin_dir, ready=True, version=version))
                    publish(bin_dir / APP, environment / "bin" / APP, previous, root, bin_dir)
                    committed = True
                    return version
                finally:
                    if not committed:
                        # A signal can arrive immediately after atomic publication.
                        # Never remove an environment the command already selects.
                        command = bin_dir / APP
                        published = (command.is_symlink()
                                     and os.readlink(command) == str(environment / "bin" / APP))
                        if not published:
                            remove_environment(environment, root, bin_dir)
    except OSError:
        raise InstallError("Installation file operation failed; check paths, ownership, permissions and free space. Existing command preserved.") from None


def uninstall(data_home, bin_dir):
    app_data = absolute_path(data_home) / APP
    root = app_data / "app"
    bin_dir = absolute_path(bin_dir)
    try:
        if not os.path.lexists(root):
            if os.path.lexists(bin_dir / APP):
                raise InstallError("Existing command has no matching ownership record; files preserved.")
            return
        directory(bin_dir)
        with lock(bin_dir / ".media-to-subs.install.lock"):
            directory(app_data)
            with lock(app_data / ".installer.lock"):
                prepare_root(root, bin_dir)
                command_state(bin_dir / APP, root, bin_dir)
                environments = list((root / "installs").iterdir())
                for environment in environments:
                    validate_environment_owner(environment, root, bin_dir)
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise InstallError("This Python cannot safely remove environments; files preserved.")
                (bin_dir / APP).unlink(missing_ok=True)
                for environment in environments:
                    remove_environment(environment, root, bin_dir)
    except OSError:
        raise InstallError("Uninstall file operation failed; check installation permissions. Profile/model assets preserved.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path,
                        default=os.environ.get("MEDIA_TO_SUBS_BIN_DIR", str(Path.home() / ".local/bin")))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--wheelhouse", type=Path, help="local wheels for dependencies/build requirements")
    parser.add_argument("--offline", action="store_true", help="use only local artifacts; disable package indexes")
    parser.add_argument("--uninstall", action="store_true", help="remove owned environments/command; preserve profiles/models")
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11) or fcntl is None or not hasattr(os, "O_NOFOLLOW"):
            raise InstallError(PREREQUISITES + " This installer supports macOS/Linux only.")
        data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        if args.uninstall:
            uninstall(data_home, args.bin_dir)
            print("Removed owned command/environments. Profiles and model assets preserved.")
            return 0
        wheelhouse = args.wheelhouse.expanduser().resolve(strict=True) if args.wheelhouse else None
        print("Installing into an isolated user environment...", flush=True)
        version = install(args.source, data_home, args.bin_dir, wheelhouse=wheelhouse, offline=args.offline)
        print(f"Installed {APP} {version}: {args.bin_dir / APP}")
        if str(args.bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
            print(f'For this shell, run: export PATH={shlex.quote(str(args.bin_dir))}:"$PATH"')
        print("media-to-subs setup\nmedia-to-subs video.mkv")
        return 0
    except (InstallError, OSError) as exc:
        message = str(exc) if isinstance(exc, InstallError) else "Cannot access an installer path; check permissions."
        print(f"Install: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Install interrupted. Any published command remains usable; rerun to verify or upgrade.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
