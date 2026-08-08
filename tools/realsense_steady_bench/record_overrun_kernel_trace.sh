#!/bin/sh
# Record the kernel-side evidence needed to diagnose SCHED_DEADLINE overruns.

set -eu

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 OUTPUT_TRACE.DAT COMMAND [ARG ...]" >&2
    exit 2
fi

output=$1
shift
tracefs=/sys/kernel/tracing
probe='rsdiag/dl_runtime_exhausted'

if [ "$(id -u)" -ne 0 ]; then
    echo "This helper must run as root (use sudo)." >&2
    exit 1
fi

case "$(uname -r)" in
    6.12.96-rpi5-standard-btf+|6.12.96-rpi5-rt-btf+|6.12.96-rpi5-rt-btf-uvc16+)
        ;;
    *)
        echo "Unsupported kernel for the instruction-offset kprobe: $(uname -r)" >&2
        exit 1
        ;;
esac

cleanup()
{
    echo "-:$probe" >> "$tracefs/kprobe_events" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

echo "-:$probe" >> "$tracefs/kprobe_events" 2>/dev/null || true
# Both archived 6.12.96 BTF builds place the post-throttle point at +0x1a0.
# x19 is sched_dl_entity here; runtime is offset 64 and the flag word is 84.
echo 'p:rsdiag/dl_runtime_exhausted update_curr_dl_se+0x1a0 runtime=+64(%x19):s64 flags=+84(%x19):u32' \
    >> "$tracefs/kprobe_events"

# Include CPU0: the isolated benchmark workers run on CPUs 1-3, while the two
# xHCI IRQs are deliberately pinned to housekeeping CPU0. Excluding CPU0 hid
# the upstream USB interrupt behavior around a userspace overrun.
# trace-cmd retains per-CPU ring data until the traced process exits. CPU0 sees
# roughly 1,300 xHCI interrupts/s with two D435 cameras, so the small default
# ring preserved only the tail of a ten-minute run. Allocate the ring before
# warmup and keep only events not already covered by the userspace V4L2 trace.
trace-cmd record \
    -C mono \
    -M f \
    -m 262144 \
    -o "$output" \
    -e rsdiag:dl_runtime_exhausted \
    -e irq:irq_handler_entry \
    -e irq:irq_handler_exit \
    -e irq:softirq_entry \
    -e irq:softirq_exit \
    -e signal:signal_generate \
    -e signal:signal_deliver \
    -- "$@"
