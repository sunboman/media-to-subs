# Installer and model setup handoff

The wizard can lazily import `DEFAULT_MANIFEST`, `load_manifest(path)`,
`fetch_model(entry, model_dir, *, timeout=30.0, max_seconds=7200.0)` and
`SetupError` from `media_to_subs.model_setup`. `DEFAULT_MANIFEST` is a package-local
`Path`, independent of the working directory. The return value of `load_manifest`
is the existing mapping from model IDs to metadata; `fetch_model` returns a `Path`.
Downloads happen only on an explicit `fetch_model` call. All original resume,
checksum, locking and publication checks remain. Repository and package manifests
must remain byte-identical (enforced by tests). The checkout script delegates to
this implementation and works with standard Python without an installed package.

Installer contract: `./install.sh` uses a verified
Python 3.11+ interpreter, a private environment under
`${XDG_DATA_HOME:-$HOME/.local/share}/media-to-subs/app`, and a command symlink under
`${MEDIA_TO_SUBS_BIN_DIR:-$HOME/.local/bin}` (also `--bin-dir PATH`). Each install
creates its environment at its final unique path, validates it, then atomically
publishes the command. Only installer-owned commands may be upgraded. No model
downloads, profile writes or shell-rc edits occur. `./install.sh --uninstall`
removes the owned command/environments and preserves profile/model assets.
`media-to-subs/models` is a sibling of `app`, outside installer ownership; a cache
created by setup from source before installation is supported. Uninstall leaves
the small ownership/lock files for safe reuse and never deletes the shared data
root. The same `--bin-dir` and XDG roots are required for upgrades/uninstall.

Python may be chosen explicitly with `MEDIA_TO_SUBS_PYTHON=/absolute/path/python3`.
Package builds use pip only inside the new venv. Build/runtime dependencies may be
downloaded from the package index; the application itself is installed from the
local source. For offline use, provide `--wheelhouse /absolute/path --offline`
with all build/runtime dependencies, or `--source /absolute/path/package.whl`.
No uv or environment activation is required. Failure output suppresses pip logs
because those may contain network credentials.

Environments stay at their final unique `app/installs/install-<id>` locations,
following Python's [venv portability guidance](https://docs.python.org/3/library/venv.html#how-venvs-work).
Earlier environments remain available across upgrades and are removed only by
scoped uninstall. Installation directories must be owned by the user, must not
be symlinks, and must not be writable by others; the owned `app` directory is 0700.
If an installation path traverses a symlink, use the real absolute directory path.
The command directory must be on PATH; otherwise the installer prints one exact
export command for the current shell. It never edits shell startup files.
