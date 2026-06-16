#!/bin/sh
set -eu

GIT_ROOT=$(git rev-parse --show-toplevel)
LIME_REPO_PATH="${LIME_REPO_PATH:-$GIT_ROOT/deps/lime-rtw}"
LIME_PATH="${LIME_PATH:-$LIME_REPO_PATH/target/release/lime-rtw}"
TRACE_OUTPUT="${TRACE_OUTPUT:-$GIT_ROOT/lime_trace_out}"
MODEL_OUTPUT="${MODEL_OUTPUT:-$GIT_ROOT/lime_model_out}"
EXTRA_ARGS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --out|-o)
      [ $# -ge 2 ] || { echo "Missing value for $1" >&2; exit 1; }
      MODEL_OUTPUT="$2"
      shift 2
      ;;
    --from|-f)
      [ $# -ge 2 ] || { echo "Missing value for $1" >&2; exit 1; }
      TRACE_OUTPUT="$2"
      shift 2
      ;;
    --best-effort|-b)
      EXTRA_ARGS="$EXTRA_ARGS --best-effort"
      shift 1
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

"$LIME_PATH" extract --from "$TRACE_OUTPUT" -o "$MODEL_OUTPUT" $EXTRA_ARGS
