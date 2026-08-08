#!/bin/sh
# Record the low-volume kernel evidence needed to localize stale framesets.

set -eu

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 OUTPUT_TRACE.DAT COMMAND [ARG ...]" >&2
    exit 2
fi

output=$1
shift
tracefs=/sys/kernel/tracing
probe_group=rsfresh

if [ "$(id -u)" -ne 0 ]; then
    echo "This helper must run as root (use sudo)." >&2
    exit 1
fi

# The dynamic probes below use BTF-verified arm64 structure offsets. Both
# archived kernels were built from the same 6.12.96 source and have identical
# uvcvideo/videobuf2 layouts. Refuse an unknown layout instead of recording
# plausible-looking but incorrect fields.
case "$(uname -r)" in
    6.12.96-rpi5-standard-btf+|6.12.96-rpi5-standard-btf-uvc16+|6.12.96-rpi5-rt-btf+|6.12.96-rpi5-rt-btf-uvc16+)
        ;;
    *)
        echo "Unsupported kernel for the UVC structure-offset probes: $(uname -r)" >&2
        exit 1
        ;;
esac

remove_probe()
{
    echo "-:$probe_group/$1" >> "$tracefs/kprobe_events" 2>/dev/null || true
}

cleanup()
{
    remove_probe uvc_frame_ready
    remove_probe uvc_frame_validation
    remove_probe uvc_buffer_error
    remove_probe uvc_no_video_buffer
}
trap cleanup EXIT HUP INT TERM

cleanup

# uvc_queue_next_buffer(queue, buf) runs once when UVC finishes a video or
# metadata frame. vb2_buffer.type distinguishes VIDEO_CAPTURE (1) from
# META_CAPTURE (13). The sequence is assigned by UVC before this call.
echo \
    'p:rsfresh/uvc_frame_ready uvc_queue_next_buffer queue=%x0 buf=%x1 type=+12(%x1):u32 sequence=+608(%x1):u32 state=+1088(%x1):u32 error=+1092(%x1):u32 bytesused=+1108(%x1):u32' \
    >> "$tracefs/kprobe_events"

# uvc_video_next_buffers(stream, &video_buf, &meta_buf) runs immediately
# before the driver's uncompressed-frame length validation. Keep only frames
# that are already marked bad or have a short/long payload. The UVC driver's
# per-frame header counters let the parser distinguish a device-set UVC ERR
# bit from an isochronous packet error or an otherwise incomplete frame. The
# tracefs filter language cannot compare two event fields, so trace-cmd stores
# this one compact event per completed frame and the parser retains only rows
# where prior_error != 0 or bytesused != expected.
echo \
    'p:rsfresh/uvc_frame_validation uvc_video_next_buffers stream=%x0 buf=+0(%x1):x64 sequence=+608(+0(%x1)):u32 prior_error=+1092(+0(%x1)):u32 bytesused=+1108(+0(%x1)):u32 expected=+1234(%x0):u32 frame_invalid_headers=+9144(%x0):u32 frame_header_errors=+9148(%x0):u32' \
    >> "$tracefs/kprobe_events"

# uvc_queue_buffer_complete receives &uvc_buffer.ref. Record only corrupted
# buffers; the successful completion path is already covered by frame_ready
# and the native VB2 tracepoints.
echo \
    'p:rsfresh/uvc_buffer_error uvc_queue_buffer_complete sequence=-508(%x0):u32 state=-28(%x0):u32 error=-24(%x0):u32 bytesused=-8(%x0):u32' \
    >> "$tracefs/kprobe_events"

# uvc_video_complete asks for the current video buffer for every completed
# URB. A NULL return means payloads arrived while the UVC irqqueue was empty.
# The tracepoint filter retains only that exceptional case.
echo \
    'r:rsfresh/uvc_no_video_buffer uvc_queue_get_current_buffer ret=$retval' \
    >> "$tracefs/kprobe_events"

# Frame-level UVC/VB2 events are small enough for a full ten-minute run. xHCI
# givebacks are retained only on error; recording every isochronous URB would
# both produce a very large trace and perturb the workload being diagnosed.
trace-cmd record \
    -C mono \
    -M f \
    -m 65536 \
    -o "$output" \
    -e rsfresh:uvc_frame_ready -f 'type == 1' \
    -e rsfresh:uvc_frame_validation \
    -e rsfresh:uvc_buffer_error -f 'error != 0' \
    -e rsfresh:uvc_no_video_buffer -f 'ret == 0' \
    -e vb2:vb2_buf_done \
    -e vb2:vb2_dqbuf \
    -e vb2:vb2_qbuf \
    -e v4l2:vb2_v4l2_dqbuf \
    -e v4l2:vb2_v4l2_qbuf \
    -e xhci-hcd:xhci_urb_giveback \
        -f 'dir_in == 1 && type == 1 && status != 0' \
    -- "$@"
