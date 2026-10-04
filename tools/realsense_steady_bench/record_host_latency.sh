#!/bin/sh
# Record only the kernel events needed for xHCI-to-frameset latency.
#
# The UVC structure offsets below apply to the archived Raspberry Pi
# 6.12.96 standard/RT BTF kernels used by the paper campaigns.

set -eu

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 OUTPUT_TRACE.DAT COMMAND [ARG ...]" >&2
    exit 2
fi

output=$1
shift
tracefs=/sys/kernel/tracing
probe_group=rshostlat

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
    if [ -e "$tracefs/events/$probe_group/enable" ]; then
        echo 0 > "$tracefs/events/$probe_group/enable" 2>/dev/null || true
    fi
    remove_probe hcd_giveback
    remove_probe uvc_complete_begin
    remove_probe uvc_complete_end
    remove_probe uvc_buffer_complete
}

trap cleanup_probes EXIT HUP INT TERM
cleanup_probes

echo 'p:rshostlat/hcd_giveback usb_hcd_giveback_urb hcd=%x0 urb=%x1 status=%x2:s32' \
    >> "$tracefs/kprobe_events"
echo 'p:rshostlat/uvc_complete_begin uvc_video_complete urb=%x0 uvc_urb=+168(%x0):x64 stream=+8(+168(%x0)):x64' \
    >> "$tracefs/kprobe_events"
echo 'r:rshostlat/uvc_complete_end uvc_video_complete' \
    >> "$tracefs/kprobe_events"

# uvc_queue_buffer_complete() receives &uvc_buffer.kref.  In the archived
# 6.12.96 modules, vb2_buffer.timestamp, sequence, and bytesused are 1092,
# 508, and 8 bytes before that kref.
echo 'p:rshostlat/uvc_buffer_complete uvc_queue_buffer_complete ref=%x0 backend_ns=-1092(%x0):s64 sequence=-508(%x0):u32 bytesused=-8(%x0):u32' \
    >> "$tracefs/kprobe_events"

irq_filter=$(awk '
    /xhci-hcd/ {
        irq=$1
        sub(/:$/, "", irq)
        if (irq ~ /^[0-9]+$/) {
            if (seen) printf " || "
            printf "irq == %s", irq
            seen=1
        }
    }
    END { if (!seen) exit 1 }
' /proc/interrupts) || {
    echo "No xHCI IRQ found in /proc/interrupts" >&2
    exit 1
}

trace-cmd record \
    -C mono \
    -M f \
    -b 32768 \
    -o "$output" \
    -e "$probe_group" \
    -e irq:irq_handler_entry -f "$irq_filter" \
    -e irq:irq_handler_exit -f "$irq_filter" \
    -e sched:sched_switch \
        -f 'prev_comm ~ "irq/*xhci*" || next_comm ~ "irq/*xhci*"' \
    -e sched:sched_wakeup \
        -f 'comm ~ "irq/*xhci*"' \
    -- "$@"
