#!/bin/sh
# Two-level reviewer entry point; independent of the caller's working directory.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
python="$repo/.venv/bin/python"
[ -x "$python" ] || python=python3
exec "$python" "$repo/reproduction/pipeline.py" "$@"
