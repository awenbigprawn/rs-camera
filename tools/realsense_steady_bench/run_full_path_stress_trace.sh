#!/bin/sh
# Run one 30-second, one-camera stress trace on the Raspberry Pi RT/UVC16 kernel.

set -eu

usage()
{
    echo "Usage: sudo $0 d435|d455 SERIAL PROFILE.csv OUTPUT_DIR [XHCI_IRQ]" >&2
    exit 2
}

[ "$#" -ge 4 ] && [ "$#" -le 5 ] || usage
model=$1
serial=$2
profile=$3
output=$4
xhci_irq=${5:-137}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this helper as root." >&2
    exit 1
fi
if [ -e "$output" ]; then
    echo "Output already exists: $output" >&2
    exit 1
fi
if [ ! -f "$profile" ]; then
    echo "Scheduler profile does not exist: $profile" >&2
    exit 1
fi

case "$model" in
    d435)
        color_width=960
        color_height=540
        ;;
    d455)
        color_width=848
        color_height=480
        ;;
    *)
        usage
        ;;
esac

repo=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
probe=$repo/build-realsense-steady/realsense_steady_probe
reset_probe=$repo/build-realsense-steady/d435_sensor_probe
interposer=$repo/build-realsense-steady/libtrace_pthreads.so
recorder=$repo/tools/realsense_steady_bench/record_full_receive_path.sh

for required in "$probe" "$reset_probe" "$interposer" "$recorder"; do
    if [ ! -e "$required" ]; then
        echo "Required build artifact is missing: $required" >&2
        exit 1
    fi
done

irq_tid=$(ps -eLo tid=,comm= | awk -v irq="$xhci_irq" \
    '$2 ~ ("^irq/" irq "-.*xhci") {print $1; exit}')
if [ -z "$irq_tid" ]; then
    echo "Cannot find threaded xHCI IRQ $xhci_irq" >&2
    exit 1
fi

mkdir -p "$output"
restore()
{
    chrt -f -p 50 "$irq_tid" 2>/dev/null || true
    "$repo/scripts/restore_cpu_freq_default.sh" >/dev/null 2>&1 || true
}
trap restore EXIT HUP INT TERM

"$repo/scripts/lock_cpu_freq.sh" 1500000 > "$output/cpu_frequency_lock.txt"
chrt -f -p 90 "$irq_tid"
{
    uname -a
    printf 'model=%s\nserial=%s\nxhci_irq=%s\nxhci_tid=%s\n' \
        "$model" "$serial" "$xhci_irq" "$irq_tid"
    chrt -p "$irq_tid"
    printf 'profile=%s\n' "$profile"
} > "$output/environment.txt"

"$reset_probe" --serial "$serial" --hardware-reset --reset-timeout-ms 5000

"$recorder" "$output/kernel_trace.dat" \
    env \
        LD_LIBRARY_PATH="$repo/build-realsense-steady/RelWithDebInfo" \
        LD_PRELOAD="$interposer" \
        RS_THREAD_TRACE_FILE="$output/thread_lifecycle.jsonl" \
        RS_V4L2_DIAGNOSTIC_TRACE_FILE="$output/v4l2_diagnostic.bin" \
        RS_V4L2_DIAGNOSTIC_TRACE_CAPACITY=1000000 \
    "$probe" \
        --serial "$serial" \
        --camera-count 1 \
        --stream-mode stereo_all \
        --delivery wait \
        --frames 1800 \
        --measurement-duration-ms 30000 \
        --warmup-frames 30 \
        --warmup-health-window-frames 10 \
        --frame-timeout-ms 1500 \
        --startup-timeout-ms 15000 \
        --fps 60 \
        --depth-width 848 \
        --depth-height 480 \
        --color-width "$color_width" \
        --color-height "$color_height" \
        --rate-monotonic-profile "$profile" \
        --rate-monotonic-policy fifo \
        --rate-monotonic-highest-priority 80 \
        --summary-output "$output/steady_summary.json" \
        --events-output "$output/frame_events.csv"

python3 "$repo/tools/realsense_steady_bench/parse_v4l2_diagnostic_trace.py" \
    --trace "$output/v4l2_diagnostic.bin" \
    --lifecycle "$output/thread_lifecycle.jsonl" \
    --output-dir "$output"

chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$output"
