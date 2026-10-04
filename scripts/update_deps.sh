#!/bin/sh
# Restore the superproject's pinned submodules, never pull moving branches.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
git -C "$repo" submodule sync --recursive
git -C "$repo" submodule update --init --recursive
python3 "$repo/scripts/check_dependencies.py" --sources
