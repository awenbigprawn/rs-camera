#!/bin/sh
set -eu

repo=${RS_CAMERA_REPO:-/home/safebot/program/rs-camera}
tool_dir="$repo/tools/realsense_steady_bench"
python="$repo/.venv/bin/python"
runner="$tool_dir/run_steady_campaign.py"
generator="$tool_dir/generate_deadline_profile.py"
irq_helper="$tool_dir/.xhci_irq_control_20260810.py"
config="$tool_dir/configs/predictive_resource_model_30s.json"
build_dir=${RS_D455_N3_BUILD_DIR:-$repo/build-realsense-steady}
other_root=${RS_D455_N3_OTHER_ROOT:-$tool_dir/results/d455_n3_split_resource_validation_20260818/results}
result_root=${RS_D455_N3_FIFO_RESULT_ROOT:-$tool_dir/results/d455_n3_split_fifo_rm_validation_20260818}
expected_kernel=${RS_D455_N3_KERNEL:-6.12.96-rpi5-rt-btf-uvc16+}
profile="$result_root/profile/d455_n3_representative30.csv"
irq_original="$result_root/environment/irq-original.json"
irq_configured=0

restore_irq()
{
    if [ "$irq_configured" -eq 1 ] && [ -f "$irq_original" ]; then
        sudo -n "$python" "$irq_helper" restore \
            --repo-root "$repo" \
            --original-input "$irq_original" \
            --output "$result_root/environment/irq-restored.json" || true
        irq_configured=0
    fi
}

cleanup()
{
    status=$?
    restore_irq
    if [ "$status" -ne 0 ]; then
        printf 'status=%s time=%s\n' "$status" "$(date --iso-8601=seconds)" \
            > "$result_root/FAILED"
    fi
    exit "$status"
}
trap cleanup 0
trap 'exit 130' 1 2 15

if [ "$(uname -r)" != "$expected_kernel" ]; then
    printf 'Expected kernel %s, found %s\n' "$expected_kernel" "$(uname -r)" >&2
    exit 2
fi

sudo -n true

if pgrep -f 'run_steady_campaign.py|realsense_steady_probe|lime-rtw trace' >/dev/null 2>&1; then
    printf '%s\n' 'Another RealSense benchmark or trace is already running.' >&2
    exit 3
fi

for camera_path in \
    /sys/bus/usb/devices/3-1.2 \
    /sys/bus/usb/devices/3-1.3 \
    /sys/bus/usb/devices/5-1.2
do
    if [ ! -f "$camera_path/idVendor" ] ||
       [ "$(cat "$camera_path/idVendor")" != 8086 ] ||
       [ "$(cat "$camera_path/idProduct")" != 0b5c ] ||
       [ "$(cat "$camera_path/speed")" != 5000 ]; then
        printf 'Expected one SuperSpeed D455 at %s\n' "$camera_path" >&2
        exit 4
    fi
done

mkdir -p "$result_root/environment" "$result_root/profile"
uname -a > "$result_root/environment/uname.txt"
cat /proc/cmdline > "$result_root/environment/cmdline.txt"
lsusb > "$result_root/environment/lsusb.txt"
lsusb -t > "$result_root/environment/lsusb-tree.txt"
git -C "$repo" rev-parse HEAD > "$result_root/environment/git-commit.txt"
git -C "$repo" status --short > "$result_root/environment/git-status.txt"
cp "$config" "$result_root/environment/cases.json"
cat > "$result_root/environment/prediction.txt" <<'EOF'
Prediction fixed before the held-out FIFO-RM runs:
child threads = 2*23 - 12 = 34
userspace CPU = 2*0.09678591611449584 - 0.04536212274333435
              = 0.14820970948565733 core equivalents
The calibration values are the medians of three prior FIFO-RM runs for one
and two D455 cameras under the same representative 30-FPS workload.
EOF

printf '%s\n' -1 | sudo -n tee /sys/module/usbcore/parameters/autosuspend >/dev/null
for camera_path in \
    /sys/bus/usb/devices/3-1.2 \
    /sys/bus/usb/devices/3-1.3 \
    /sys/bus/usb/devices/5-1.2
do
    printf '%s\n' on | sudo -n tee "$camera_path/power/control" >/dev/null
done

set --
find "$other_root" -type f -name selected_attempt.txt \
    -path '*/policy-other/*/run-*/*' | sort \
    > "$result_root/profile/other-traces.txt"
trace_count=$(wc -l < "$result_root/profile/other-traces.txt" | tr -d ' ')
if [ "$trace_count" -ne 3 ]; then
    printf 'Expected three selected SCHED_OTHER traces, found %s\n' \
        "$trace_count" >&2
    exit 5
fi
while IFS= read -r selection; do
    set -- "$@" --trace-run "$(dirname "$selection")"
done < "$result_root/profile/other-traces.txt"
"$python" "$generator" "$@" --output "$profile" \
    > "$result_root/profile/generator.json"

sudo -n "$python" "$irq_helper" configure \
    --repo-root "$repo" --cpus keep --policy SCHED_FIFO --priority 90 \
    --original-output "$irq_original" \
    --effective-output "$result_root/environment/irq-effective-before.json"
irq_configured=1

"$python" "$runner" \
    --config "$config" \
    --case validate_d455_n3_split_representative30 \
    --policies fifo-rm \
    --scheduler-profile "$profile" \
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

sudo -n "$python" "$irq_helper" verify \
    --repo-root "$repo" --cpus keep --policy SCHED_FIFO --priority 90 \
    --output "$result_root/environment/irq-effective-after.json"
restore_irq
date --iso-8601=seconds > "$result_root/DONE"
trap - 0
