#!/bin/sh
set -eu

repo=${RS_CAMERA_REPO:-/home/safebot/program/rs-camera}
tool_dir="$repo/tools/realsense_steady_bench"
runner="$tool_dir/run_steady_campaign.py"
config="$tool_dir/configs/predictive_resource_model_30s.json"
python="$repo/.venv/bin/python"
build_dir=${RS_CAPACITY_LADDER_BUILD_DIR:-$repo/build-realsense-steady}
result_root=${RS_CAPACITY_LADDER_RESULT_ROOT:-$tool_dir/results/predictive_capacity_ladder_20260818}
expected_kernel=6.12.96-rpi5-rt-btf-uvc16+

mkdir -p "$result_root/environment"

disable_camera_autosuspend()
{
    printf '%s\n' -1 | sudo -n tee /sys/module/usbcore/parameters/autosuspend \
        >/dev/null
    for device in /sys/bus/usb/devices/*; do
        [ -f "$device/idVendor" ] || continue
        [ "$(cat "$device/idVendor")" = 8086 ] || continue
        case "$(cat "$device/idProduct")" in
            0ad3|0b07|0b5c)
                printf '%s\n' on | sudo -n tee "$device/power/control" \
                    >/dev/null
                ;;
        esac
    done
}

record_environment()
{
    uname -a > "$result_root/environment/uname.txt"
    cat /proc/cmdline > "$result_root/environment/cmdline.txt"
    lsusb > "$result_root/environment/lsusb.txt"
    lsusb -t > "$result_root/environment/lsusb-tree.txt"
    git -C "$repo" rev-parse HEAD > "$result_root/environment/git-commit.txt"
    git -C "$repo" status --short > "$result_root/environment/git-status.txt"
    cp "$config" "$result_root/environment/cases.json"
    printf '%s\n' \
        'Four-camera capacity ladder: depth-only 60 FPS, depth plus IR 30 FPS, and depth plus color 60 FPS.' \
        'Each cell has one LiME topology run and three low-overhead repetitions where startup succeeds.' \
        'All runs use SCHED_OTHER, the original CPU affinities, and no CPU isolation.' \
        > "$result_root/environment/design.txt"
}

run_cell()
{
    case_id=$1
    trace_mode=$2
    repetitions=$3
    output="$result_root/$case_id/$trace_mode"

    [ ! -f "$output/DONE" ] || return 0
    mkdir -p "$output"
    disable_camera_autosuspend

    trace_args=
    if [ "$trace_mode" = low-overhead ]; then
        trace_args=--no-lime
    fi

    status=0
    # trace_args contains either no argument or the fixed --no-lime flag.
    # shellcheck disable=SC2086
    "$python" "$runner" \
        --config "$config" --case "$case_id" \
        --policies other --nb-runs "$repetitions" \
        --measurement-duration-seconds 30 \
        --recover-on-failure full-reset --reset-before-run \
        --max-attempts-per-run 3 --recovery-settle-seconds 0 \
        --build-jobs 3 --build-dir "$build_dir" \
        --backend v4l2 --no-cpu-isolation \
        --v4l2-diagnostics-build-only \
        $trace_args \
        --results-dir "$output/results" || status=$?

    if [ "$status" -eq 0 ]; then
        date --iso-8601=seconds > "$output/DONE"
    else
        printf 'status=%s time=%s\n' "$status" "$(date --iso-8601=seconds)" \
            > "$output/FAILED"
    fi
    return 0
}

sudo -n true
if [ "$(uname -r)" != "$expected_kernel" ]; then
    printf 'Expected kernel %s, found %s\n' "$expected_kernel" "$(uname -r)" >&2
    exit 2
fi

record_environment
disable_camera_autosuspend
for case_id in \
    validate_mixed_n4_depth_only60 \
    validate_mixed_n4_depth_ir30 \
    validate_mixed_n4_depth_color60
do
    run_cell "$case_id" lime 1
    run_cell "$case_id" low-overhead 3
done
date --iso-8601=seconds > "$result_root/COMPLETE"
