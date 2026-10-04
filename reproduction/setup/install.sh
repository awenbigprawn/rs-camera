#!/bin/sh
# Run on the Pi as the regular experiment user.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
if [ "${1:-}" = --help ]; then
    echo 'Usage: sh reproduction/setup/install.sh [installer options]'
    echo 'Reuses scripts/install_dependencies_ubuntu.sh; builds both benchmark directories.'
    exit 0
fi
sh "$repo/scripts/install_dependencies_ubuntu.sh" --locked-system --v4l2-diagnostics --build --gpu-noise --build-dir "$repo/build-realsense-steady" "$@"
cmake --build "$repo/build-realsense-steady" --target realsense_steady_probe --parallel "${BUILD_JOBS:-3}"
sh "$repo/scripts/install_dependencies_ubuntu.sh" --locked-system --build --skip-apt-update --build-dir "$repo/build-realsense-thread-trace" "$@"
sh "$repo/tools/realsense_steady_bench/build_uvc_worker_fifo_monitor.sh" /tmp/rs-p1-v2-uvc-worker-monitor

# Tracing and kernel tools were included in the locked system profile.
sudo install -m 0644 "$repo/deps/librealsense/config/99-realsense-libusb.rules" /etc/udev/rules.d/99-realsense-libusb.rules
sudo udevadm control --reload-rules
printf '%s\n' 'Connect cameras, then run: python3 reproduction/setup/discover.py'
