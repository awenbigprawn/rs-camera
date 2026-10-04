#!/bin/sh
# Install the complete locked Python environment; do not resolve new versions.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
venv=${VENV_DIR:-$repo/.venv}
python=${PYTHON:-python3}
"$python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Use Python 3.12 for the paper environment"'
if [ ! -x "$venv/bin/python" ]; then
    "$python" -m venv "$venv"
fi
"$venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Existing venv must use Python 3.12"'
"$venv/bin/python" "$repo/scripts/check_dependencies.py" --sources
"$venv/bin/python" -m pip install --no-deps -r "$repo/dependencies/python-build.txt"
"$venv/bin/python" -m pip install --no-deps --no-build-isolation -r "$repo/requirements.txt"
"$venv/bin/python" -m pip install --no-deps --no-build-isolation -e "$repo/deps/benchkit"
"$venv/bin/python" -m pip check
"$venv/bin/python" "$repo/scripts/check_dependencies.py" --sources --python --build-tools
