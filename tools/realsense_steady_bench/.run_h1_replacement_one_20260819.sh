#!/bin/sh

set -eu

if [ "$#" -ne 5 ]; then
    echo "usage: $0 CAMERA TREATMENT REP ATTEMPT RESULT_ROOT" >&2
    exit 2
fi

camera=$1
treatment=$2
rep=$3
attempt=$4
matrix_root=$5

repo=/home/safebot/program/rs-camera
tool_dir="$repo/tools/realsense_steady_bench"
monitor_build=/tmp/rs-uvc-worker-monitor-build
monitor="$monitor_build/uvc_worker_fifo_monitor"
monitor_object="$monitor_build/uvc_worker_fifo_monitor.bpf.o"
fault_cpu=3

case "$camera" in
    d435)
        serial=948122073863
        config="$tool_dir/.d1_h1_single_d435_20260819.json"
        case_id=d1_h1_d435_stress60
        profile="$tool_dir/results/d1_h1_single_d435_20260819/profiles/stress.csv"
        xhci_irq=132
        xhci_label='xhci-hcd:usb2'
        ;;
    d455)
        serial=311322302503
        config="$tool_dir/.h1_single_d455_20260819.json"
        case_id=h1_d455_stress60
        profile="$tool_dir/results/h1_replacement_profiles/d455_stress60_fullpath.csv"
        xhci_irq=137
        xhci_label='xhci-hcd:usb4'
        ;;
    *)
        echo "unsupported camera: $camera" >&2
        exit 2
        ;;
esac

case "$treatment" in
    xhci-other_uvc-other)
        xhci_policy=other
        use_uvc_monitor=no
        ;;
    xhci-fifo90_uvc-other)
        xhci_policy=fifo90
        use_uvc_monitor=no
        ;;
    xhci-fifo90_uvc-fifo89)
        xhci_policy=fifo90
        use_uvc_monitor=yes
        ;;
    *)
        echo "unsupported treatment: $treatment" >&2
        exit 2
        ;;
esac

run_root="$matrix_root/camera-$camera/treatment-$treatment/rep-$rep/attempt-$attempt"
mkdir -p "$run_root"

xhci_tid=
campaign_pid=
fault_job=
monitor_pid=
xhci_changed=no

restore_xhci()
{
    [ "$xhci_changed" = yes ] || return 0
    printf '%s\n' "$original_irq_affinity" | sudo -n tee \
        "/proc/irq/$xhci_irq/smp_affinity_list" >/dev/null || true
    sudo -n taskset -pc "$original_task_affinity" "$xhci_tid" >/dev/null || true
    case "$original_policy" in
        SCHED_OTHER) sudo -n chrt -o -p 0 "$xhci_tid" >/dev/null || true ;;
        SCHED_FIFO) sudo -n chrt -f -p "$original_priority" "$xhci_tid" >/dev/null || true ;;
        SCHED_RR) sudo -n chrt -r -p "$original_priority" "$xhci_tid" >/dev/null || true ;;
    esac
    xhci_changed=no
}

cleanup()
{
    status=$?
    trap - 0 1 2 15
    if [ -n "$fault_job" ]; then
        sudo -n pkill -TERM -f rs_xhci_fault_worker 2>/dev/null || true
        wait "$fault_job" 2>/dev/null || true
    fi
    if [ -n "$monitor_pid" ]; then
        sudo -n pkill -TERM -x uvc_worker_fifo_monitor 2>/dev/null || true
        wait "$monitor_pid" 2>/dev/null || true
    fi
    if [ -n "$campaign_pid" ]; then
        kill -0 "$campaign_pid" 2>/dev/null && kill -TERM "$campaign_pid" 2>/dev/null || true
        wait "$campaign_pid" 2>/dev/null || true
    fi
    restore_xhci
    if [ "$status" -ne 0 ]; then
        printf 'status=%s\ntime=%s\n' "$status" "$(date --iso-8601=seconds)" \
            > "$run_root/FAILED"
    fi
    exit "$status"
}
trap cleanup 0
trap 'exit 130' 1 2 15

sudo -n true
test -r "$config"
test -r "$profile"
grep -q "^[[:space:]]*$xhci_irq:.*$xhci_label" /proc/interrupts
if [ "$use_uvc_monitor" = yes ]; then
    test -x "$monitor"
    test -r "$monitor_object"
fi

xhci_tid=$(ps -eLo tid=,comm= | awk -v irq="$xhci_irq" \
    '$2 ~ ("^irq/" irq "-xhci") {print $1; exit}')
test -n "$xhci_tid"

chrt -p "$xhci_tid" > "$run_root/xhci_original_chrt.txt"
taskset -pc "$xhci_tid" > "$run_root/xhci_original_affinity.txt"
cat "/proc/irq/$xhci_irq/smp_affinity_list" > "$run_root/xhci_original_irq_affinity.txt"
original_policy=$(awk -F: '/scheduling policy/ {gsub(/^[ \t]+/, "", $2); print $2}' \
    "$run_root/xhci_original_chrt.txt")
original_priority=$(awk -F: '/scheduling priority/ {gsub(/^[ \t]+/, "", $2); print $2}' \
    "$run_root/xhci_original_chrt.txt")
original_task_affinity=$(sed -n 's/.*: //p' "$run_root/xhci_original_affinity.txt")
original_irq_affinity=$(cat "$run_root/xhci_original_irq_affinity.txt")

printf '%s\n' "$fault_cpu" | sudo -n tee "/proc/irq/$xhci_irq/smp_affinity_list" >/dev/null
sudo -n taskset -pc "$fault_cpu" "$xhci_tid" >/dev/null
case "$xhci_policy" in
    other) sudo -n chrt -o -p 0 "$xhci_tid" ;;
    fifo90) sudo -n chrt -f -p 90 "$xhci_tid" ;;
esac
xhci_changed=yes

{
    uname -a
    cat /proc/cmdline
    printf 'camera=%s\nserial=%s\ncase_id=%s\nrep=%s\nattempt=%s\n' \
        "$camera" "$serial" "$case_id" "$rep" "$attempt"
    printf 'xhci_irq=%s\nxhci_tid=%s\nxhci_cpu=%s\nxhci_policy=%s\n' \
        "$xhci_irq" "$xhci_tid" "$fault_cpu" "$xhci_policy"
    printf 'uvc_worker_policy=%s\n' \
        "$(if [ "$use_uvc_monitor" = yes ]; then echo SCHED_FIFO_89; else echo SCHED_OTHER; fi)"
    printf 'fault_cpu=%s\nfault_policy=SCHED_RR\nfault_priority=1\n' "$fault_cpu"
    printf 'fault_busy_ms=4.5\nfault_sleep_ms=0.5\nfault_duty_percent=90\n'
    printf 'sdk_policy=FIFO-RM\nmeasurement_seconds=30\nfull_path_trace=true\n'
} > "$run_root/experiment_configuration.txt"

cd "$repo"
.venv/bin/python "$tool_dir/run_steady_campaign.py" \
    --config "$config" \
    --case "$case_id" \
    --policies fifo-rm \
    --scheduler-profile "$profile" \
    --nb-runs 1 \
    --measurement-duration-seconds 30 \
    --serial "$serial" \
    --recover-on-failure full-reset \
    --reset-before-run \
    --max-attempts-per-run 1 \
    --recovery-settle-seconds 0 \
    --cpu-noise-modes busy_loop \
    --cpu-noise-workers 1 \
    --cpu-noise-warmup-seconds 1 \
    --cpu-noise-ready-timeout-seconds 15 \
    --build-jobs 3 \
    --build-dir "$repo/build-realsense-steady" \
    --backend v4l2 \
    --no-cpu-isolation \
    --full-path-kernel-trace \
    --results-dir "$run_root" \
    > "$run_root/campaign.log" 2>&1 &
campaign_pid=$!

warmup_marker=
while [ -z "$warmup_marker" ]; do
    if ! kill -0 "$campaign_pid" 2>/dev/null; then
        wait "$campaign_pid"
        exit 1
    fi
    warmup_marker=$(find "$run_root" -type f -name camera_warmup_ready -print -quit)
    [ -n "$warmup_marker" ] || sleep 0.05
done
attempt_dir=$(dirname "$warmup_marker")

if [ "$use_uvc_monitor" = yes ]; then
    sudo -n "$monitor" \
        --bpf-object "$monitor_object" \
        --priority 89 \
        --duration 40 \
        --log "$attempt_dir/uvc_worker_fifo_monitor.jsonl" \
        > "$attempt_dir/uvc_worker_fifo_monitor.stdout" \
        2> "$attempt_dir/uvc_worker_fifo_monitor.stderr" &
    monitor_pid=$!
    sleep 0.25
    kill -0 "$monitor_pid"
fi

(
    set +e
    date --iso-8601=ns > "$run_root/fault_start_time.txt"
    sudo -n taskset -c "$fault_cpu" chrt -r 1 python3 -c \
        "import time; exec('end=time.monotonic()+40.0\\nwhile time.monotonic()<end:\\n stop=time.perf_counter()+0.0045\\n while time.perf_counter()<stop: pass\\n time.sleep(0.0005)')" \
        rs_xhci_fault_worker \
        > "$run_root/fault_stdout.txt" \
        2> "$run_root/fault_stderr.txt"
    fault_status=$?
    printf '%s\n' "$fault_status" > "$run_root/fault_returncode.txt"
    date --iso-8601=ns > "$run_root/fault_end_time.txt"
    exit "$fault_status"
) &
fault_job=$!

wait "$campaign_pid"
campaign_pid=
wait "$fault_job"
fault_job=
if [ "$use_uvc_monitor" = yes ]; then
    wait "$monitor_pid"
    monitor_pid=
fi

test "$(cat "$run_root/fault_returncode.txt")" = 0
test -s "$attempt_dir/full_receive_path_summary.json"
test -s "$attempt_dir/kernel_trace.dat"
test -s "$attempt_dir/frame_events.csv"
if [ "$use_uvc_monitor" = yes ]; then
    grep -q '"action":"promoted"' "$attempt_dir/uvc_worker_fifo_monitor.jsonl"
    grep -q '"action":"restored"' "$attempt_dir/uvc_worker_fifo_monitor.jsonl"
fi

restore_xhci
rm -f "$run_root/FAILED"
printf '%s\n' "$attempt_dir" > "$run_root/selected_attempt.txt"
printf 'completed camera=%s treatment=%s rep=%s attempt=%s\n' \
    "$camera" "$treatment" "$rep" "$attempt"
