#!/bin/sh
# Two-level reviewer entry point; independent of the caller's working directory.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
venv=${VENV_DIR:-$repo/.venv}
python="$venv/bin/python"

# Keep help and campaign controls usable without installing dependencies.
bootstrap=false
skip_value=false
for argument do
    if [ "$skip_value" = true ]; then
        skip_value=false
        continue
    fi
    case "$argument" in
        -h|--help) bootstrap=false; break ;;
        --input|--output|--config|--calibration-seconds) skip_value=true ;;
        raw|full) bootstrap=true ;;
    esac
done
if [ "$bootstrap" = true ]; then
    if [ ! -x "$python" ] || ! "$python" "$repo/scripts/check_dependencies.py" --python --build-tools >/dev/null 2>&1; then
        printf '%s\n' "Preparing the locked Python environment: $venv"
        VENV_DIR="$venv" sh "$repo/scripts/install_python.sh"
    fi
    exec "$python" "$repo/reproduction/pipeline.py" "$@"
fi
[ -x "$python" ] || python=python3
exec "$python" "$repo/reproduction/pipeline.py" "$@"
