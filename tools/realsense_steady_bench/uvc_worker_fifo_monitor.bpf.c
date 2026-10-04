// SPDX-License-Identifier: GPL-2.0
// Report the kworker that executes deferred uvcvideo frame-copy work.

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

struct worker_event
{
    __u32 tid;
    __u32 tgid;
    __u64 timestamp_ns;
    char comm[16];
};

struct
{
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, __u32);
    __type(value, __u8);
} reported SEC(".maps");

struct
{
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);
} events SEC(".maps");

SEC("kprobe/uvc_video_copy_data_work")
int report_uvc_copy_worker(void *ctx)
{
    const __u64 pid_tgid = bpf_get_current_pid_tgid();
    const __u32 tid = (__u32)pid_tgid;
    const __u8 one = 1;
    struct worker_event *event;

    (void)ctx;
    if (bpf_map_lookup_elem(&reported, &tid))
        return 0;

    event = bpf_ringbuf_reserve(&events, sizeof(*event), 0);
    if (!event)
        return 0;

    event->tid = tid;
    event->tgid = pid_tgid >> 32;
    event->timestamp_ns = bpf_ktime_get_ns();
    bpf_get_current_comm(event->comm, sizeof(event->comm));
    bpf_ringbuf_submit(event, 0);
    bpf_map_update_elem(&reported, &tid, &one, BPF_ANY);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
