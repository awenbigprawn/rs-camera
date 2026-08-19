#!/bin/sh
set -eu

repo=${RS_CAMERA_REPO:-/home/safebot/program/rs-camera}
tool_dir="$repo/tools/realsense_steady_bench"
runner="$tool_dir/run_steady_campaign.py"
generator="$tool_dir/generate_deadline_profile.py"
irq_helper="$tool_dir/.xhci_irq_control_20260810.py"
config="$tool_dir/configs/predictive_resource_model_30s.json"
python="$repo/.venv/bin/python"
build_dir=${RS_POLICY_MODEL_BUILD_DIR:-$repo/build-realsense-steady}
prerequisite_root=${RS_POLICY_MODEL_PREREQUISITE_ROOT:-$tool_dir/results/predictive_model_20260818}
result_root=${RS_POLICY_MODEL_RESULT_ROOT:-$tool_dir/results/policy_time_model_validation_20260818}
prerequisite_unit=${RS_POLICY_MODEL_PREREQUISITE_UNIT:-rs-predictive-resource-model.service}
expected_kernel=6.12.96-rpi5-rt-btf-uvc16+
current_irq_original=

mkdir -p "$result_root/environment" "$result_root/profiles"

restore_irq()
{
    if [ -n "$current_irq_original" ] && [ -f "$current_irq_original" ]; then
        sudo -n "$python" "$irq_helper" restore \
            --repo-root "$repo" \
            --original-input "$current_irq_original" \
            --output "$result_root/environment/irq-restored.json" || true
        current_irq_original=
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

wait_for_calibration()
{
    while systemctl is-active --quiet "$prerequisite_unit"; do
        sleep 15
    done
    if [ ! -f "$prerequisite_root/SCREENING_COMPLETE" ]; then
        printf 'Prerequisite experiment did not complete: %s\n' \
            "$prerequisite_root" >&2
        return 1
    fi
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
        'Purpose: test whether role-aware scheduling preserves the SCHED_OTHER worker-family and activation-period model.' \
        'Treatments: SCHED_OTHER, RR-RM, FIFO-RM, and SCHED_DEADLINE; three 30-second runs per feasible calibration case.' \
        'The two camera-controller xHCI IRQ threads remain SCHED_FIFO priority 90 with their original CPU affinities.' \
        'No CPU affinity, taskset, cgroup partition, or CPU isolation is applied to the benchmark process.' \
        > "$result_root/environment/design.txt"
}

generate_profile()
{
    case_id=$1
    source="$result_root/cases/$case_id/other/results"
    profile="$result_root/profiles/$case_id.csv"
    trace_list="$result_root/profiles/$case_id.trace-runs.txt"

    find "$source" -type f -name selected_attempt.txt \
        -path '*/policy-other/*/run-*/*' | sort > "$trace_list"
    trace_count=$(wc -l < "$trace_list" | tr -d ' ')
    if [ "$trace_count" -ne 3 ]; then
        printf 'Expected three selected SCHED_OTHER traces for %s, found %s\n' \
            "$case_id" "$trace_count" >&2
        return 1
    fi

    set --
    while IFS= read -r selection; do
        set -- "$@" --trace-run "$(dirname "$selection")"
    done < "$trace_list"
    "$python" "$generator" "$@" --output "$profile" \
        > "$result_root/profiles/$case_id.generator.json"
}

run_policy_case()
{
    case_id=$1
    policy=$2
    profile="$result_root/profiles/$case_id.csv"

    output="$result_root/cases/$case_id/$policy"
    [ ! -f "$output/DONE" ] || return 0
    mkdir -p "$output"
    disable_camera_autosuspend

    profile_args=
    if [ "$policy" != other ]; then
        profile_args="--scheduler-profile $profile"
    fi

    # profile_args contains only the generated profile path above.
    # shellcheck disable=SC2086
    "$python" "$runner" \
        --config "$config" --case "$case_id" \
        --policies "$policy" --nb-runs 3 \
        --measurement-duration-seconds 30 \
        --recover-on-failure full-reset --reset-before-run \
        --max-attempts-per-run 3 --recovery-settle-seconds 0 \
        --build-jobs 3 --build-dir "$build_dir" \
        --backend v4l2 --no-cpu-isolation \
        --v4l2-diagnostics-build-only \
        $profile_args \
        --results-dir "$output/results"
    date --iso-8601=seconds > "$output/DONE"
}

sudo -n true
wait_for_calibration

if [ "$(uname -r)" != "$expected_kernel" ]; then
    printf 'Expected kernel %s, found %s\n' "$expected_kernel" "$(uname -r)" >&2
    exit 2
fi

current_irq_original="$result_root/environment/irq-original.json"
sudo -n "$python" "$irq_helper" configure \
    --repo-root "$repo" --cpus keep --policy SCHED_FIFO --priority 90 \
    --original-output "$current_irq_original" \
    --effective-output "$result_root/environment/irq-effective-before.json"

record_environment
disable_camera_autosuspend

# Collect a contemporaneous OTHER baseline after fixing the xHCI predecessor
# policy. These traces are the only inputs to the modeled-policy profiles.
for case_id in \
    predict_d435_n1_representative30 \
    predict_d435_n1_common_stress60 \
    predict_d455_n1_representative30 \
    predict_d455_n1_common_stress60 \
    predict_d455_n2_representative30
do
    run_policy_case "$case_id" other
    generate_profile "$case_id"
done

for policy in rr-rm fifo-rm deadline; do
    for case_id in \
        predict_d435_n1_representative30 \
        predict_d435_n1_common_stress60 \
        predict_d455_n1_representative30 \
        predict_d455_n1_common_stress60 \
        predict_d455_n2_representative30
    do
        run_policy_case "$case_id" "$policy"
    done
done

sudo -n "$python" "$irq_helper" verify \
    --repo-root "$repo" --cpus keep --policy SCHED_FIFO --priority 90 \
    --output "$result_root/environment/irq-effective-after.json"
restore_irq
date --iso-8601=seconds > "$result_root/COMPLETE"
trap - 0
