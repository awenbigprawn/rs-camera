#!/bin/sh
set -eu

GIT_ROOT=$(git rev-parse --show-toplevel)
LIME_REPO_PATH="${LIME_REPO_PATH:-$GIT_ROOT/deps/lime-rtw}"
LIME_PATH="${LIME_PATH:-$LIME_REPO_PATH/target/release/lime-rtw}"
MODEL_OUTPUT="${MODEL_OUTPUT:-$GIT_ROOT/lime_model_out}"

while [ $# -gt 0 ]; do
  case "$1" in
    --from|-f)
      [ $# -ge 2 ] || { echo "Missing value for $1" >&2; exit 1; }
      MODEL_OUTPUT="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ ! -d "$LIME_REPO_PATH" ]; then
  echo "lime-rtw repo not found at $LIME_REPO_PATH" >&2
  echo "Set LIME_REPO_PATH=/path/to/lime-rtw or add deps/lime-rtw." >&2
  exit 1
fi

if [ ! -x "$LIME_PATH" ]; then
  (cd "$LIME_REPO_PATH" && cargo build --release)
fi

if [ ! -x "$LIME_PATH" ]; then
  echo "lime-rtw not found at $LIME_PATH" >&2
  exit 1
fi

"$LIME_PATH" view "$MODEL_OUTPUT"
