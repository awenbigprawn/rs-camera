#!/bin/sh
set -eu

GIT_ROOT=$(git rev-parse --show-toplevel)
LIME_REPO_PATH="${LIME_REPO_PATH:-$GIT_ROOT/deps/lime-rtw}"
LIME_PATH="${LIME_PATH:-$LIME_REPO_PATH/target/release/lime-rtw}"
BUILD_DIR="${BUILD_DIR:-$GIT_ROOT/build}"
EXEC_NAME="${EXEC_NAME:-d435_sensor_probe}"
EXEC_PATH="$BUILD_DIR/$EXEC_NAME"
OUTPUT="${OUTPUT:-$GIT_ROOT/lime_trace_out}"
ADDITIONAL_ARGS=""

usage() {
  echo "usage: $0 [--exec d435_sensor_probe|smallest_test] [--out PATH] [--best-effort] [-- executable-args...]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --exec|-e)
      [ $# -ge 2 ] || { usage; exit 1; }
      EXEC_NAME="$2"
      EXEC_PATH="$BUILD_DIR/$EXEC_NAME"
      shift 2
      ;;
    --out|-o)
      [ $# -ge 2 ] || { usage; exit 1; }
      OUTPUT="$2"
      shift 2
      ;;
    --best-effort|-b)
      ADDITIONAL_ARGS="$ADDITIONAL_ARGS --best-effort"
      shift 1
      ;;
    --)
      shift 1
      break
      ;;
    *)
      break
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

cmake --build "$BUILD_DIR" --target "$EXEC_NAME" --parallel "$(nproc)"
"$GIT_ROOT/scripts/setcap.sh" "$EXEC_PATH"

if [ ! -x "$EXEC_PATH" ]; then
  echo "$EXEC_NAME not found at $EXEC_PATH" >&2
  exit 1
fi

sudo "$LIME_PATH" trace -o "$OUTPUT" $ADDITIONAL_ARGS -- "$EXEC_PATH" "$@"
