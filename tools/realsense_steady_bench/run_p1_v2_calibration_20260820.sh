#!/bin/sh
set -eu

repo=/home/safebot/program/rs-camera
tool_dir="$repo/tools/realsense_steady_bench"
python="$repo/.venv/bin/python"
runner="$tool_dir/run_steady_campaign.py"
generator="$tool_dir/generate_deadline_profile.py"
config="$tool_dir/p1_v2_cases_20260820.json"
helper="$tool_dir/.xhci_irq_control_20260810.py"
build_dir="$repo/build-realsense-steady"
monitor_build=/tmp/rs-p1-v2-uvc-worker-monitor
monitor="$monitor_build/uvc_worker_fifo_monitor"
monitor_object="$monitor_build/uvc_worker_fifo_monitor.bpf.o"
result_root="$tool_dir/results/p1_cross_layer_v2_20260820/calibration"
profiles="$tool_dir/results/p1_cross_layer_v2_20260820/profiles"
irq_original="$result_root/irq_original.json"
monitor_pid=
irq_configured=no

cleanup()
{
    status=$?
    trap - 0 1 2 15
    if [ -n "$monitor_pid" ]; then
        sudo -n kill -TERM "$monitor_pid" 2>/dev/null || true
        wait "$monitor_pid" 2>/dev/null || true
    fi
    if [ "$irq_configured" = yes ]; then
        sudo -n "$python" "$helper" restore \
            --repo-root "$repo" \
            --original-input "$irq_original" \
            --output "$result_root/irq_restored.json" || true
    fi
    if [ "$status" -ne 0 ]; then
        printf 'status=%s\ntime=%s\n' "$status" "$(date --iso-8601=seconds)" \
            > "$result_root/FAILED"
    fi
    exit "$status"
}
trap cleanup 0
trap 'exit 130' 1 2 15

disable_autosuspend()
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

start_monitor()
{
    workload=$1
    log="$result_root/$workload/uvc_worker_monitor.jsonl"
    sudo -n "$monitor" \
        --bpf-object "$monitor_object" \
        --policy fifo --priority 89 --duration 0 --log "$log" \
        > "$result_root/$workload/uvc_worker_monitor.stdout" \
        2> "$result_root/$workload/uvc_worker_monitor.stderr" &
    monitor_pid=$!
    sleep 1
    kill -0 "$monitor_pid"
}

stop_monitor()
{
    sudo -n kill -TERM "$monitor_pid"
    wait "$monitor_pid"
    monitor_pid=
}

run_workload()
{
    workload=$1
    case "$workload" in
        representative) case_id=p1v2_d435_n2_representative30 ;;
        stress) case_id=p1v2_d435_n2_stress60 ;;
        *) return 2 ;;
    esac
    output="$result_root/$workload"
    mkdir -p "$output"
    start_monitor "$workload"
    "$python" "$runner" \
        --config "$config" --case "$case_id" \
        --policies other --nb-runs 3 \
        --measurement-duration-seconds 30 \
        --recover-on-failure full-reset --reset-before-run \
        --max-attempts-per-run 3 --recovery-settle-seconds 0 \
        --build-jobs 3 --build-dir "$build_dir" \
        --backend v4l2 --no-cpu-isolation \
        --v4l2-diagnostics-build-only \
        --results-dir "$output/results"
    stop_monitor

    set -- "$python" "$generator"
    run_count=0
    for run in $(find "$output/results" -type f -name selected_attempt.txt \
        -printf '%h\n' | sort); do
        set -- "$@" --trace-run "$run"
        run_count=$((run_count + 1))
    done
    [ "$run_count" -eq 3 ]
    "$@" --output "$profiles/$workload.csv" \
        --runtime-margin 1.20 --period-scale 0.91 \
        > "$output/profile_generation.json"
}

sudo -n true
[ "$(uname -r)" = 6.12.96-rpi5-standard-btf-uvc16+ ]
grep -qw threadirqs /proc/cmdline
[ ! -e "$result_root/STARTED" ]
mkdir -p "$result_root" "$profiles"
date --iso-8601=seconds > "$result_root/STARTED"
uname -a > "$result_root/uname.txt"
cat /proc/cmdline > "$result_root/cmdline.txt"
git -C "$repo" rev-parse HEAD > "$result_root/git_commit.txt"
git -C "$repo" status --short > "$result_root/git_status.txt"
disable_autosuspend

sudo -n "$python" "$helper" configure \
    --repo-root "$repo" --cpus keep \
    --policy SCHED_FIFO --priority 90 \
    --original-output "$irq_original" \
    --effective-output "$result_root/irq_effective.json"
irq_configured=yes

run_workload representative
run_workload stress

for workload in representative stress; do
    test -s "$profiles/$workload.csv"
    test -s "$profiles/$workload.csv.json"
    grep -q '"action":"promoted"' "$result_root/$workload/uvc_worker_monitor.jsonl"
    ! grep -q '"action":"promote-failed"' "$result_root/$workload/uvc_worker_monitor.jsonl"
done
date --iso-8601=seconds > "$result_root/COMPLETED"
