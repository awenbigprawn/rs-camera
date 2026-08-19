#!/bin/sh
set -eu

repo=${RS_CAMERA_REPO:-/home/safebot/program/rs-camera}
tool_dir="$repo/tools/realsense_steady_bench"
runner="$tool_dir/run_steady_campaign.py"
config="$tool_dir/configs/predictive_resource_model_30s.json"
python="$repo/.venv/bin/python"
result_root=${RS_D455_N3_RESULT_ROOT:-$tool_dir/results/d455_n3_split_resource_validation_20260818}
build_dir=${RS_D455_N3_BUILD_DIR:-$repo/build-realsense-steady}
expected_kernel=${RS_D455_N3_KERNEL:-6.12.96-rpi5-rt-btf-uvc16+}

if [ "$(uname -r)" != "$expected_kernel" ]; then
    printf 'Expected kernel %s, found %s\n' "$expected_kernel" "$(uname -r)" >&2
    exit 2
fi

sudo -n true

if pgrep -f 'run_steady_campaign.py|realsense_steady_probe|lime-rtw trace' >/dev/null 2>&1; then
    printf '%s\n' 'Another RealSense benchmark or trace is already running.' >&2
    exit 3
fi

camera_count=0
for camera_path in /sys/bus/usb/devices/3-1.2 /sys/bus/usb/devices/3-1.3 /sys/bus/usb/devices/5-1.2; do
    if [ ! -f "$camera_path/idVendor" ] ||
       [ "$(cat "$camera_path/idVendor")" != 8086 ] ||
       [ "$(cat "$camera_path/idProduct")" != 0b5c ] ||
       [ "$(cat "$camera_path/speed")" != 5000 ]; then
        printf 'Expected one SuperSpeed D455 at %s\n' "$camera_path" >&2
        exit 4
    fi
    camera_count=$((camera_count + 1))
done

if [ "$camera_count" -ne 3 ]; then
    printf 'Expected three D455 cameras, found %s\n' "$camera_count" >&2
    exit 4
fi

printf '%s\n' -1 | sudo -n tee /sys/module/usbcore/parameters/autosuspend >/dev/null
for camera_path in /sys/bus/usb/devices/3-1.2 /sys/bus/usb/devices/3-1.3 /sys/bus/usb/devices/5-1.2; do
    printf '%s\n' on | sudo -n tee "$camera_path/power/control" >/dev/null
done

mkdir -p "$result_root/environment"
uname -a > "$result_root/environment/uname.txt"
cat /proc/cmdline > "$result_root/environment/cmdline.txt"
lsusb > "$result_root/environment/lsusb.txt"
lsusb -t > "$result_root/environment/lsusb-tree.txt"
git -C "$repo" rev-parse HEAD > "$result_root/environment/git-commit.txt"
git -C "$repo" status --short > "$result_root/environment/git-status.txt"
cp "$config" "$result_root/environment/cases.json"

"$python" "$runner" \
    --config "$config" \
    --case validate_d455_n3_split_representative30 \
    --policies other \
    --nb-runs 3 \
    --measurement-duration-seconds 30 \
    --recover-on-failure full-reset \
    --reset-before-run \
    --max-attempts-per-run 3 \
    --recovery-settle-seconds 0 \
    --build-jobs 3 \
    --build-dir "$build_dir" \
    --backend v4l2 \
    --no-cpu-isolation \
    --v4l2-diagnostics-build-only \
    --results-dir "$result_root/results"

date --iso-8601=seconds > "$result_root/DONE"
