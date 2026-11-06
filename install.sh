#!/bin/sh
# Bootstrap only; all installation and subprocess handling is in standard Python.
set -eu
installer_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if [ -n "${MEDIA_TO_SUBS_PYTHON:-}" ]; then
    if "$MEDIA_TO_SUBS_PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$MEDIA_TO_SUBS_PYTHON" -I "$installer_dir/scripts/install.py" "$@"
    fi
    echo 'MEDIA_TO_SUBS_PYTHON must name a working Python 3.11+ executable.' >&2
    exit 1
fi
for candidate in python3 python3.15 python3.14 python3.13 python3.12 python3.11; do
    if "$candidate" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$candidate" -I "$installer_dir/scripts/install.py" "$@"
    fi
done
echo 'Python 3.11+ is required. macOS: brew install python. Debian/Ubuntu: install python3 and python3-venv (Python 3.11+), then rerun ./install.sh.' >&2
exit 1
