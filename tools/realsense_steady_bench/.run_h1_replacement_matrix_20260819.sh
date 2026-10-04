#!/bin/sh

set -eu

repo=/home/safebot/program/rs-camera
tool_dir="$repo/tools/realsense_steady_bench"
one="$tool_dir/.run_h1_replacement_one_20260819.sh"
root="$tool_dir/results/h1_replacement_targeted_starvation_20260819"

mkdir -p "$root"
test -x "$one"
sudo -n true

{
    date --iso-8601=seconds
    uname -a
    cat /proc/cmdline
    printf 'design=2 cameras x 3 treatments x 3 repetitions\n'
    printf 'workload=60 FPS model-specific stress profile\n'
    printf 'sdk=FIFO-RM\nmeasurement_seconds=30\nwarmup_frames=30\n'
    printf 'targeted_starvation=CPU3 SCHED_RR/1 4.5ms busy + 0.5ms sleep\n'
    printf 'generic_cpu_noise=one SCHED_OTHER register-only busy-loop worker\n'
    printf 'uvc_urbs=16\nfull_path_trace=true\n'
} > "$root/matrix_configuration.txt"

# Interleave cameras and treatments to limit time-order bias.  Each line is
# CAMERA:TREATMENT:REPETITION.
matrix='d435:xhci-other_uvc-other:1
d455:xhci-fifo90_uvc-other:1
d435:xhci-fifo90_uvc-fifo89:1
d455:xhci-other_uvc-other:1
d435:xhci-fifo90_uvc-other:1
d455:xhci-fifo90_uvc-fifo89:1
d455:xhci-fifo90_uvc-fifo89:2
d435:xhci-fifo90_uvc-other:2
d455:xhci-other_uvc-other:2
d435:xhci-fifo90_uvc-fifo89:2
d455:xhci-fifo90_uvc-other:2
d435:xhci-other_uvc-other:2
d435:xhci-fifo90_uvc-other:3
d455:xhci-other_uvc-other:3
d435:xhci-fifo90_uvc-fifo89:3
d455:xhci-fifo90_uvc-other:3
d435:xhci-other_uvc-other:3
d455:xhci-fifo90_uvc-fifo89:3'

printf '%s\n' "$matrix" | while IFS=: read -r camera treatment rep; do
    selected_file="$root/camera-$camera/treatment-$treatment/rep-$rep/SELECTED"
    if [ -s "$selected_file" ]; then
        selected=$(cat "$selected_file")
        if find "$selected" -type f -name full_receive_path_summary.json \
            -size +0c -print -quit | grep -q .; then
            printf '[H1] already complete camera=%s treatment=%s rep=%s\n' \
                "$camera" "$treatment" "$rep"
            continue
        fi
    fi
    success=no
    attempt=1
    while [ "$attempt" -le 3 ]; do
        printf '[H1] camera=%s treatment=%s rep=%s attempt=%s\n' \
            "$camera" "$treatment" "$rep" "$attempt"
        if "$one" "$camera" "$treatment" "$rep" "$attempt" "$root"; then
            selected="$root/camera-$camera/treatment-$treatment/rep-$rep/attempt-$attempt"
            printf '%s\n' "$selected" \
                > "$root/camera-$camera/treatment-$treatment/rep-$rep/SELECTED"
            success=yes
            break
        fi
        attempt=$((attempt + 1))
        sleep 2
    done
    if [ "$success" != yes ]; then
        printf 'failed camera=%s treatment=%s rep=%s\n' \
            "$camera" "$treatment" "$rep" > "$root/MATRIX_FAILED"
        exit 1
    fi
    sleep 2
done

rm -f "$root/MATRIX_FAILED"
date --iso-8601=seconds > "$root/MATRIX_COMPLETE"
printf 'H1 replacement matrix completed: %s\n' "$root"
