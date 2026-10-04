#!/bin/sh
# Re-run C1 for one D435 and one D455 with LiME and the full Linux receive path.

set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this helper as root." >&2
    exit 1
fi

repo=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
output=${1:-$repo/tools/realsense_steady_bench/results/c1_full_receive_path_20260819}
probe=$repo/build-realsense-steady/realsense_steady_probe
reset_probe=$repo/build-realsense-steady/d435_sensor_probe
interposer=$repo/build-realsense-steady/libtrace_pthreads.so
recorder=$repo/tools/realsense_steady_bench/record_full_receive_path.sh
v4l2_parser=$repo/tools/realsense_steady_bench/parse_v4l2_diagnostic_trace.py
steady_parser=$repo/tools/realsense_steady_bench/parse_steady_trace.py
path_analyzer=$repo/tools/realsense_steady_bench/analyze_full_receive_path.py
lime=$repo/deps/lime-rtw/target/release/lime-rtw

d435_serial=948122073863
d455_serial=311322302503
max_attempts=3

for required in "$probe" "$reset_probe" "$interposer" "$recorder" \
    "$steady_parser" "$v4l2_parser" "$path_analyzer" "$lime"; do
    if [ ! -e "$required" ]; then
        echo "Required artifact is missing: $required" >&2
        exit 1
    fi
done

mkdir -p "$output"
manifest=$output/manifest.tsv
if [ ! -e "$manifest" ]; then
    printf 'model\tworkload\trepetition\tselected_attempt\tsuccess\tresult_dir\n' \
        > "$manifest"
fi

irq_tids=$(ps -eLo tid=,comm= | awk '$2 ~ /^irq\/[0-9]+-xhci/ {print $1}')
if [ -z "$irq_tids" ]; then
    echo "No threaded xHCI IRQ workers were found." >&2
    exit 1
fi

restore()
{
    for tid in $irq_tids; do
        chrt -f -p 50 "$tid" >/dev/null 2>&1 || true
    done
    "$repo/scripts/restore_cpu_freq_default.sh" >/dev/null 2>&1 || true
}
trap restore EXIT HUP INT TERM

"$repo/scripts/lock_cpu_freq.sh" 1500000 > "$output/cpu_frequency_lock.txt"
for tid in $irq_tids; do
    chrt -f -p 90 "$tid"
done

{
    date --iso-8601=seconds
    uname -a
    printf 'build_type=%s\n' \
        "$(sed -n 's/^CMAKE_BUILD_TYPE:STRING=//p' "$repo/build-realsense-steady/CMakeCache.txt")"
    printf 'librealsense_backend=V4L2\nuvc_urbs=16\n'
    printf 'userspace_policy=SCHED_OTHER\nxhci_policy=SCHED_FIFO\nxhci_priority=90\n'
    printf 'measurement_seconds=30\nwarmup_frames=30\nrepetitions=3\n'
    printf 'd435_serial=%s\nd455_serial=%s\n' "$d435_serial" "$d455_serial"
    printf 'xhci_tids=%s\n' "$(printf '%s' "$irq_tids" | tr '\n' ' ')"
    ps -eLo tid,cls,rtprio,psr,comm,args | grep -E 'irq/[0-9]+-xhci' || true
    lsusb -t
} > "$output/environment.txt"

run_case()
{
    model=$1
    workload=$2
    repetition=$3

    case "$model" in
        d435)
            serial=$d435_serial
            stress_color_width=960
            stress_color_height=540
            ;;
        d455)
            serial=$d455_serial
            stress_color_width=848
            stress_color_height=480
            ;;
        *)
            echo "Unsupported camera model: $model" >&2
            return 2
            ;;
    esac

    case "$workload" in
        representative30)
            stream_mode=depth_color
            fps=30
            color_width=640
            color_height=480
            frame_capacity=1000
            ;;
        stress60)
            stream_mode=stereo_all
            fps=60
            color_width=$stress_color_width
            color_height=$stress_color_height
            frame_capacity=2000
            ;;
        *)
            echo "Unsupported workload: $workload" >&2
            return 2
            ;;
    esac

    logical_dir=$output/$model/$workload/run-$repetition
    if [ -s "$logical_dir/selected_attempt.txt" ]; then
        selected=$(cat "$logical_dir/selected_attempt.txt")
        if python3 -c 'import json,sys; raise SystemExit(not json.load(open(sys.argv[1]))["success"])' \
            "$logical_dir/attempt-$selected/steady_summary.json"; then
            echo "[C1] skip completed $model $workload repetition=$repetition attempt=$selected"
            return 0
        fi
    fi

    mkdir -p "$logical_dir"
    attempt=1
    while [ "$attempt" -le "$max_attempts" ]; do
        attempt_dir=$logical_dir/attempt-$attempt
        if [ -e "$attempt_dir" ]; then
            attempt=$((attempt + 1))
            continue
        fi
        mkdir -p "$attempt_dir/lime_trace"
        echo "[C1] begin $model $workload repetition=$repetition attempt=$attempt"

        sync
        echo 3 > /proc/sys/vm/drop_caches
        dmesg_lines=$(dmesg | wc -l)

        {
            date --iso-8601=seconds
            uname -a
            printf 'model=%s\nworkload=%s\nserial=%s\nrepetition=%s\nattempt=%s\n' \
                "$model" "$workload" "$serial" "$repetition" "$attempt"
            printf 'stream_mode=%s\nfps=%s\ndepth=848x480\ncolor=%sx%s\n' \
                "$stream_mode" "$fps" "$color_width" "$color_height"
            printf 'cpu_frequency_khz='; cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq
            ps -eLo tid,cls,rtprio,psr,comm,args | grep -E 'irq/[0-9]+-xhci' || true
            lsusb -t
        } > "$attempt_dir/environment.txt"

        reset_ok=true
        if ! "$reset_probe" --serial "$serial" --hardware-reset \
            --reset-timeout-ms 5000 > "$attempt_dir/reset.log" 2>&1; then
            reset_ok=false
        fi

        run_ok=false
        if [ "$reset_ok" = true ]; then
            if "$recorder" "$attempt_dir/kernel_trace.dat" \
                "$lime" trace --best-effort -o "$attempt_dir/lime_trace" -- \
                env \
                    LD_PRELOAD="$interposer" \
                    RS_THREAD_TRACE_FILE="$attempt_dir/thread_lifecycle.jsonl" \
                    RS_V4L2_DIAGNOSTIC_TRACE_FILE="$attempt_dir/v4l2_diagnostic.bin" \
                    RS_V4L2_DIAGNOSTIC_TRACE_CAPACITY=1000000 \
                chrt --other 0 \
                "$probe" \
                    --serial "$serial" \
                    --camera-count 1 \
                    --stream-mode "$stream_mode" \
                    --delivery wait \
                    --frames "$frame_capacity" \
                    --measurement-duration-ms 30000 \
                    --warmup-frames 30 \
                    --warmup-health-window-frames 10 \
                    --frame-timeout-ms 1500 \
                    --startup-timeout-ms 15000 \
                    --fps "$fps" \
                    --depth-width 848 \
                    --depth-height 480 \
                    --color-width "$color_width" \
                    --color-height "$color_height" \
                    --summary-output "$attempt_dir/steady_summary.json" \
                    --events-output "$attempt_dir/frame_events.csv"; then
                run_ok=true
            fi
        fi

        dmesg | tail -n "+$((dmesg_lines + 1))" > "$attempt_dir/kernel_log.txt" || true

        if [ -s "$attempt_dir/thread_lifecycle.jsonl" ] && \
            [ -d "$attempt_dir/lime_trace" ]; then
            python3 "$steady_parser" \
                --lifecycle "$attempt_dir/thread_lifecycle.jsonl" \
                --lime-dir "$attempt_dir/lime_trace" \
                --output-dir "$attempt_dir" >/dev/null || true
        fi

        if [ -s "$attempt_dir/v4l2_diagnostic.bin" ] && \
            [ -s "$attempt_dir/thread_lifecycle.jsonl" ]; then
            python3 "$v4l2_parser" \
                --trace "$attempt_dir/v4l2_diagnostic.bin" \
                --lifecycle "$attempt_dir/thread_lifecycle.jsonl" \
                --output-dir "$attempt_dir" || true
        fi

        valid=false
        if [ "$run_ok" = true ] && \
            [ -s "$attempt_dir/kernel_trace.dat" ] && \
            [ -s "$attempt_dir/frame_events.csv" ] && \
            [ -s "$attempt_dir/thread_lifecycle.jsonl" ] && \
            [ -s "$attempt_dir/thread_steady_summary.json" ] && \
            [ -s "$attempt_dir/v4l2_diagnostic_events.csv" ] && \
            [ -d "$attempt_dir/lime_trace" ] && \
            python3 -c 'import json,sys; raise SystemExit(not json.load(open(sys.argv[1]))["success"])' \
                "$attempt_dir/steady_summary.json"; then
            valid=true
        fi

        if [ "$valid" = true ]; then
            python3 "$path_analyzer" "$attempt_dir" || true
            printf '%s\n' "$attempt" > "$logical_dir/selected_attempt.txt"
            printf '%s\t%s\t%s\t%s\ttrue\t%s\n' \
                "$model" "$workload" "$repetition" "$attempt" "$attempt_dir" \
                >> "$manifest"
            echo "[C1] success $model $workload repetition=$repetition attempt=$attempt"
            return 0
        fi

        echo "[C1] failed $model $workload repetition=$repetition attempt=$attempt"
        attempt=$((attempt + 1))
    done

    printf '%s\t%s\t%s\t-\tfalse\t%s\n' \
        "$model" "$workload" "$repetition" "$logical_dir" >> "$manifest"
    return 1
}

failures=0
for repetition in 1 2 3; do
    for case_id in \
        d435:representative30 \
        d455:representative30 \
        d435:stress60 \
        d455:stress60; do
        model=${case_id%%:*}
        workload=${case_id#*:}
        if ! run_case "$model" "$workload" "$repetition"; then
            failures=$((failures + 1))
        fi
    done
done

restore
trap - EXIT HUP INT TERM
chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$output"

if [ "$failures" -ne 0 ]; then
    echo "[C1] completed with $failures failed logical run(s)" >&2
    exit 1
fi

echo "[C1] all 12 logical runs completed successfully: $output"
