#!/bin/sh
# Record a short RealSense kernel receive-path trace around one probe command.
#
# The UVC structure offsets below are for the archived Raspberry Pi
# 6.12.96 standard/RT BTF kernels in this repository's experiment setup.

set -eu

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 OUTPUT_TRACE.DAT COMMAND [ARG ...]" >&2
    exit 2
fi

output=$1
shift
tracefs=/sys/kernel/tracing
probe_group=rsfullpath
sampler_pid=
tasks_output=${output%.dat}_tasks.csv

if [ "$(id -u)" -ne 0 ]; then
    echo "This helper must run as root." >&2
    exit 1
fi
if [ ! -w "$tracefs/kprobe_events" ]; then
    echo "tracefs is unavailable or not writable: $tracefs" >&2
    exit 1
fi

remove_probe()
{
    echo "-:$probe_group/$1" >> "$tracefs/kprobe_events" 2>/dev/null || true
}

cleanup_probes()
{
    remove_probe usb_bh_begin
    remove_probe usb_bh_end
    remove_probe hcd_giveback
    remove_probe uvc_complete_begin
    remove_probe uvc_complete_end
    remove_probe uvc_copy_begin
    remove_probe uvc_copy_end
    remove_probe uvc_buffer_complete
    remove_probe uvc_frame_boundary
}

cleanup()
{
    if [ -n "$sampler_pid" ]; then
        kill "$sampler_pid" 2>/dev/null || true
        wait "$sampler_pid" 2>/dev/null || true
    fi
    cleanup_probes
}
trap cleanup EXIT HUP INT TERM

cleanup_probes

echo 'p:rsfullpath/usb_bh_begin usb_giveback_urb_bh work=%x0' \
    >> "$tracefs/kprobe_events"
echo 'r:rsfullpath/usb_bh_end usb_giveback_urb_bh' \
    >> "$tracefs/kprobe_events"
echo 'p:rsfullpath/hcd_giveback usb_hcd_giveback_urb hcd=%x0 urb=%x1 status=%x2:s32' \
    >> "$tracefs/kprobe_events"
echo 'p:rsfullpath/uvc_complete_begin uvc_video_complete urb=%x0 uvc_urb=+168(%x0):x64 stream=+8(+168(%x0)):x64' \
    >> "$tracefs/kprobe_events"
echo 'r:rsfullpath/uvc_complete_end uvc_video_complete' \
    >> "$tracefs/kprobe_events"
echo 'p:rsfullpath/uvc_copy_begin uvc_video_copy_data_work work=%x0' \
    >> "$tracefs/kprobe_events"
echo 'r:rsfullpath/uvc_copy_end uvc_video_copy_data_work' \
    >> "$tracefs/kprobe_events"

# uvc_queue_buffer_complete() receives &uvc_buffer.kref.  In the archived
# 6.12.96 modules, vb2_buffer.timestamp, sequence, state, error, and bytesused
# are respectively 1092, 508, 28, 24, and 8 bytes before that kref.
echo 'p:rsfullpath/uvc_buffer_complete uvc_queue_buffer_complete ref=%x0 backend_ns=-1092(%x0):s64 sequence=-508(%x0):u32 state=-28(%x0):u32 error=-24(%x0):u32 bytesused=-8(%x0):u32' \
    >> "$tracefs/kprobe_events"
echo 'p:rsfullpath/uvc_frame_boundary uvc_queue_next_buffer queue=%x0 buf=%x1 sequence=+608(%x1):u32 state=+1088(%x1):u32 error=+1092(%x1):u32 bytesused=+1108(%x1):u32' \
    >> "$tracefs/kprobe_events"

printf '%s\n' 'boottime_s,pid,tid,cpu,class,rtprio,priority,nice,comm' > "$tasks_output"
(
    while :; do
        now=$(cut -d ' ' -f 1 /proc/uptime)
        ps -eLo pid=,tid=,psr=,cls=,rtprio=,pri=,ni=,comm= | \
            awk -v now="$now" \
                '$8 ~ /^(irq\/.*xhci|ksoftirqd\/|kworker\/u|realsense_stead|rs-wait-)/ {print now "," $1 "," $2 "," $3 "," $4 "," $5 "," $6 "," $7 "," $8}'
        sleep 0.10
    done
) >> "$tasks_output" &
sampler_pid=$!

trace-cmd record \
    -C mono \
    -M f \
    -m 16384 \
    -o "$output" \
    -e "$probe_group" \
    -e irq:irq_handler_entry \
    -e irq:irq_handler_exit \
    -e irq:softirq_raise -f 'vec == 0' \
    -e irq:softirq_entry -f 'vec == 0' \
    -e irq:softirq_exit -f 'vec == 0' \
    -e workqueue:workqueue_queue_work \
    -e workqueue:workqueue_execute_start \
    -e workqueue:workqueue_execute_end \
    -e sched:sched_switch \
        -f 'prev_comm ~ "kworker/u*" || next_comm ~ "kworker/u*" || prev_comm ~ "irq/*xhci*" || next_comm ~ "irq/*xhci*" || prev_comm ~ "ksoftirqd/*" || next_comm ~ "ksoftirqd/*" || prev_comm ~ "realsense*" || next_comm ~ "realsense*" || prev_comm ~ "rs-wait-*" || next_comm ~ "rs-wait-*"' \
    -e sched:sched_wakeup \
        -f 'comm ~ "kworker/u*" || comm ~ "irq/*xhci*" || comm ~ "ksoftirqd/*" || comm ~ "realsense*" || comm ~ "rs-wait-*"' \
    -e vb2:vb2_buf_done \
    -e vb2:vb2_dqbuf \
    -e vb2:vb2_qbuf \
    -e v4l2:vb2_v4l2_dqbuf \
    -e v4l2:vb2_v4l2_qbuf \
    -e xhci-hcd:xhci_urb_giveback -f 'dir_in == 1 && type == 1' \
    -- "$@"
