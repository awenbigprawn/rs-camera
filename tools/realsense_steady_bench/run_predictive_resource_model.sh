#!/bin/sh
set -eu

repo=${RS_CAMERA_REPO:-/home/safebot/program/rs-camera}
tool_dir="$repo/tools/realsense_steady_bench"
runner="$tool_dir/run_steady_campaign.py"
config="$tool_dir/configs/predictive_resource_model_30s.json"
python="$repo/.venv/bin/python"
result_root=${RS_PREDICTIVE_RESULT_ROOT:-$tool_dir/results/predictive_model_20260818}
build_dir=${RS_PREDICTIVE_BUILD_DIR:-$repo/build-realsense-steady}

expected_kernel=6.12.96-rpi5-rt-btf-uvc16+
if [ "$(uname -r)" != "$expected_kernel" ]; then
    printf 'Expected kernel %s, found %s\n' "$expected_kernel" "$(uname -r)" >&2
    exit 2
fi

mkdir -p "$result_root/environment"

disable_camera_autosuspend()
{
    printf '%s\n' -1 | sudo -n tee /sys/module/usbcore/parameters/autosuspend >/dev/null
    for device in /sys/bus/usb/devices/*; do
        [ -f "$device/idVendor" ] || continue
        [ "$(cat "$device/idVendor")" = 8086 ] || continue
        case "$(cat "$device/idProduct")" in
            0ad3|0b07|0b5c)
                printf '%s\n' on | sudo -n tee "$device/power/control" >/dev/null
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
    ps -eLo pid,tid,cls,rtprio,pri,psr,comm,args \
        > "$result_root/environment/threads-before.txt"
    cp "$config" "$result_root/environment/cases.json"
}

run_case()
{
    case_id=$1
    repetitions=$2
    trace_mode=$3
    noise_kind=$4
    noise_level=$5
    output=$6

    [ ! -f "$output/DONE" ] || return 0
    mkdir -p "$output"
    disable_camera_autosuspend

    trace_args=
    if [ "$trace_mode" = low-overhead ]; then
        trace_args=--no-lime
    fi

    noise_args=
    case "$noise_kind" in
        none)
            ;;
        cpu)
            noise_args="--cpu-noise-modes busy_loop --cpu-noise-workers $noise_level --cpu-noise-warmup-seconds 2 --cpu-noise-ready-timeout-seconds 15"
            ;;
        memory)
            noise_args="--memory-noise-modes fixed_copy --memory-noise-workers 3 --memory-noise-target-mib-per-second $noise_level --memory-noise-copy-chunk-kib 1024 --memory-noise-warmup-seconds 2 --memory-noise-ready-timeout-seconds 15"
            ;;
        *)
            printf 'Unknown noise kind: %s\n' "$noise_kind" >&2
            return 2
            ;;
    esac

    # trace_args and noise_args contain only arguments assembled above.
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
        $trace_args $noise_args \
        --results-dir "$output/results"

    disable_camera_autosuspend
    date --iso-8601=seconds > "$output/DONE"
}

run_calibration()
{
    for case_id in \
        predict_d435_n1_representative30 \
        predict_d435_n1_common_stress60 \
        predict_d455_n1_representative30 \
        predict_d455_n1_common_stress60 \
        predict_d455_n2_representative30
    do
        run_case "$case_id" 3 lime none 0 \
            "$result_root/calibration/$case_id"
    done
}

run_four_camera_baseline()
{
    case_id=validate_mixed_n4_representative30
    run_case "$case_id" 1 lime none 0 \
        "$result_root/validation/topology-trace/$case_id"
    run_case "$case_id" 3 low-overhead none 0 \
        "$result_root/validation/baseline/$case_id"
}

run_cpu_screening()
{
    case_id=validate_mixed_n4_representative30
    for workers in 1 2 3 4
    do
        output="$result_root/validation/cpu/$case_id/workers-$workers"
        if ! run_case "$case_id" 1 low-overhead cpu "$workers" "$output"; then
            date --iso-8601=seconds > "$output/FAILED"
        fi
    done
}

run_memory_screening()
{
    case_id=validate_mixed_n4_representative30
    for target in 250 500 750 1000
    do
        output="$result_root/validation/memory/$case_id/target-$target"
        if ! run_case "$case_id" 1 low-overhead memory "$target" "$output"; then
            date --iso-8601=seconds > "$output/FAILED"
        fi
    done
}

sudo -n true
record_environment
disable_camera_autosuspend
run_calibration
run_four_camera_baseline
run_cpu_screening
run_memory_screening
date --iso-8601=seconds > "$result_root/SCREENING_COMPLETE"
