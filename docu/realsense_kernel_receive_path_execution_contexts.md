# Kernel Execution Contexts in the RealSense V4L2 Receive Path

## 1. Question and Main Result

This document identifies the kernel threads and execution contexts that carry
a D400 frame from a USB completion event to a V4L2 buffer returned to
userspace through Linux `uvcvideo` and V4L2.

The receive path is neither executed entirely by an xHCI IRQ thread nor split
into one kernel thread per stage. At least three kinds of schedulable execution
context participate in normal video reception:

1. xHCI interrupt processing in hard-IRQ context or in an
   `irq/<N>-xhci-hc` thread;
2. USB URB giveback in high-priority softirq/BH context, which may borrow the
   current task name or run in `ksoftirqd/<CPU>`; and
3. UVC asynchronous copying in dynamic high-priority unbound
   `kworker/uXX:Y` workers.

`vb2` does not add a dedicated completion thread. The final UVC copy operation
calls `vb2_buffer_done()` inline, adds the V4L2 buffer to the done queue, and
wakes the librealsense capture worker blocked in `select()`/`DQBUF`. Later
`VIDIOC_DQBUF`, callback, and `VIDIOC_QBUF` activity belongs to that userspace
capture task, including the kernel CPU time of its system calls.

The steady receive path is therefore:

```text
camera packet
  -> xHCI DMA writes host memory                         [hardware, no CPU thread]
  -> xHCI event processing                              [hardirq or irq/N-xhci-hc]
  -> usb_giveback_urb_bh                                 [HI_SOFTIRQ / ksoftirqd/N]
  -> uvc_video_complete: parse UVC header/FID/EOF        [same BH context]
  -> uvc_video_copy_data_work: copy payload              [kworker/uXX:Y, nice -20]
  -> uvc_queue_buffer_complete -> vb2_buffer_done        [last copy worker]
  -> wake librealsense capture worker
  -> select/DQBUF -> callback -> synchronization         [librealsense worker]
  -> application wait_for_frames returns                [application wait worker]
  -> QBUF returns buffer to vb2/uvcvideo                 [librealsense worker]
```

## 2. Scope and Reproducibility

Source inspection used the Raspberry Pi 5 kernel tree retained at:

- source: `build-rpi-kernel/source-uvc16`;
- commit: `ae161617d7a4552fa91d03626b8d9f3696d17481`;
- `git describe`: `ae161617d-dirty`; and
- receive-path modification: `UVC_URBS=16` in
  `drivers/media/usb/uvc/uvcvideo.h`.

Runtime evidence came primarily from:

- `results/freshness_path_diagnostics/uvc16_freshness_other_10min_v2/.../freshness_kernel_trace.dat`;
- `results/rpi5/xhci_deadline_calibration_20260811/.../xhci_sched_trace.dat`; and
- the 30-s joint discovery traces in
  `results/kernel_receive_discovery_20260817/`.

The first trace used `6.12.96-rpi5-standard-btf-uvc16+`. The August 17 joint
trace on the same kernel added `workqueue_execute_start/end` and covered a
D435, a D455, and concurrent operation. It therefore links dynamic
`kworker/uXX:Y` activations directly to `uvc_video_copy_data_work` on the
standard kernel. The same identity should be verified separately for the
PREEMPT_RT softirq and workqueue implementation.

The scope is the normal steady V4L2 video receive path. Enumeration, reset,
disconnect, runtime power management, and recovery can wake other kernel
workers but are not fixed stages in every frame's causal path.

## 3. Source-Level Path

### 3.1 xHCI DMA Is Hardware Work

The xHCI controller first writes USB payload into host memory by DMA. DMA does
not consume a Linux task's execution time, although it contends for the memory
interconnect and DRAM. The CPU subsequently consumes transfer-completion
events from the xHCI event ring.

### 3.2 xHCI Event Processing

The Raspberry Pi 5 cameras connect to RP1 xHCI controllers. Their handler is
`xhci_irq()`:

- `build-rpi-kernel/source-uvc16/drivers/usb/host/xhci.c:5473`; and
- `build-rpi-kernel/source-uvc16/drivers/usb/host/xhci.c:5481`.

xHCI registers `HCD_BH`:

```c
.irq = xhci_irq,
.flags = HCD_MEMORY | HCD_DMA | HCD_USB3 | HCD_SHARED | HCD_BH,
```

The handler therefore defers complete UVC callback processing to a bottom
half instead of invoking the complete UVC path directly.

Its schedulable identity depends on the kernel:

- standard kernel without `threadirqs`: hard-IRQ context, with no xHCI task
  that can independently receive `chrt` settings;
- standard kernel with `threadirqs`: an `irq/<N>-xhci-hc` task; and
- PREEMPT_RT: most IRQs are threaded and appear as `irq/<N>-xhci-hc`.

Raspberry Pi traces contained `irq/132-xhci-hc` and `irq/137-xhci-hc` for two
controllers. Experiments assigned these threads either `SCHED_OTHER` or
`SCHED_FIFO:90`. A PREEMPT_RT threaded IRQ often defaults to
`SCHED_FIFO:50`, but every experiment should record the observed policy and
priority instead of assuming a default.

### 3.3 USB URB Giveback Bottom Half

After xHCI handles a completion, USB core reaches `usb_hcd_giveback_urb()`:

- `build-rpi-kernel/source-uvc16/drivers/usb/core/hcd.c:1663`; and
- `build-rpi-kernel/source-uvc16/drivers/usb/core/hcd.c:1720`.

Isochronous and interrupt URBs use the HCD high-priority bottom half:

```c
if (usb_pipeisoc(urb->pipe) || usb_pipeint(urb->pipe))
    bh = &hcd->high_prio_bh;
...
queue_work(system_bh_highpri_wq, &bh->bh);
```

`system_bh_highpri_wq` is created at
`build-rpi-kernel/source-uvc16/kernel/workqueue.c:7881` with
`WQ_BH | WQ_HIGHPRI`. The `WQ_BH` definition at
`include/linux/workqueue.h:369` specifies execution in bottom-half/softirq
context.

Consequently, `usb_giveback_urb_bh()` is not a normal process-context kworker.
It executes in `HI_SOFTIRQ` and invokes `urb->complete(urb)`, which is
`uvc_video_complete()` for the RealSense video stream.

Task names can be misleading:

- A standard kernel may execute softirq inline on IRQ return, so a trace can
  show `<idle>-0` or the interrupted task. The idle task did not actively
  initiate camera processing.
- Deferred softirq work runs in per-CPU `ksoftirqd/<CPU>`.
- On PREEMPT_RT, an IRQ exit with pending softirq normally wakes
  `ksoftirqd/<CPU>`, although a BH re-enabled from another preemptible context
  can still execute inline. Interpret `softirq_entry/exit` events rather than
  task names alone.

Relevant code is in `kernel/softirq.c:245`, `:439`, and `:939`.

### 3.4 `uvc_video_complete()`: Header and Boundary Processing

The callback at `drivers/media/usb/uvc/uvc_video.c:1697` runs in the USB
giveback BH context. It:

1. checks URB status;
2. synchronizes the DMA buffer for CPU access;
3. parses the UVC payload header;
4. checks FID, EOF, and error state;
5. updates metadata;
6. records asynchronous copy operations for larger payloads; and
7. queues copy work on the stream's `async_wq`.

Header parsing and frame-boundary decisions therefore contribute to USB
BH/softirq CPU demand, not to xHCI IRQ-thread or librealsense userspace demand.

A small metadata buffer can complete in this BH path. One trace placed
metadata `vb2_buf_done(type=13)` in `<idle>-0`, followed by video
`vb2_buf_done(type=1)` in `kworker/u27:*` for the same frame.

### 3.5 UVC Asynchronous Copy Workers

Each `struct uvc_streaming` owns a workqueue named `uvcvideo`, created at
`drivers/media/usb/uvc/uvc_driver.c:209` and `:224` as:

```c
alloc_workqueue("uvcvideo", WQ_UNBOUND | WQ_HIGHPRI, 0);
```

`uvc_video_complete()` queues `uvc_urb->work`; the work function at
`uvc_video.c:1302`:

1. copies payload from the URB buffer into the current V4L2/vb2 frame buffer;
2. releases each copy operation's reference to the frame buffer; and
3. resubmits the completed URB.

Large frame-assembly copies therefore execute in dynamic unbound kworkers, not
inside the xHCI IRQ thread.

`WQ_HIGHPRI` is not a real-time scheduler policy. The high-priority worker pool
uses `MIN_NICE`, or nice -20, as defined at `kernel/workqueue.c:127`, but stays
in `SCHED_OTHER`. A scheduler trace showed:

```text
irq/132-xhci-hc [120] ==> kworker/u25:0 [100]
```

Lower scheduler-priority numbers represent higher effective priority here, so
a nice -20 UVC worker can preempt a nice-0 xHCI thread demoted to
`SCHED_OTHER`.

A stream-specific workqueue object does not imply a permanent dedicated
kernel thread. `WQ_UNBOUND` draws from a dynamic shared `kworker/uXX:Y` pool.
TIDs, suffixes, and CPUs change, and a worker may execute unrelated work. It is
unsafe to assign every task matching `kworker/u*` a camera-specific policy.

### 3.6 V4L2/vb2 Completion Is Inline

Each asynchronous copy releases a reference. The last reference calls:

- `uvc_queue_buffer_complete()` at `drivers/media/usb/uvc/uvc_queue.c:472`;
  and
- `vb2_buffer_done()` at
  `drivers/media/common/videobuf2/videobuf2-core.c:1182`.

`vb2_buffer_done()` adds the buffer to `done_list` and wakes `done_wq`. It does
not create or wake a dedicated continuation thread; it remains in the last
copy worker's task context.

A 10-minute UVC16 trace counted video `vb2_buf_done(type=1)` contexts as:

| Execution context | Count |
|---|---:|
| `kworker/u27:1` | 31,613 |
| `kworker/u27:2` | 18,776 |
| `kworker/u27:0` | 15,269 |
| `kworker/u27:3` | 7,551 |
| `<idle>` | 16 |

Thus almost every normal video completion ran in unbound kworkers. In
contrast, most `uvc_frame_validation` records appeared as `<idle>-0`, matching
the preceding IRQ-return softirq stage.

### 3.7 System Calls in the librealsense Capture Worker

The V4L2 backend calls `select()` and then `VIDIOC_DQBUF` in its capture worker:

- `deps/librealsense/src/linux/backend-v4l2.cpp:1775`; and
- `deps/librealsense/src/linux/backend-v4l2.cpp:1885`.

After release, it invokes `VIDIOC_QBUF` at `backend-v4l2.cpp:419` and `:434`.
The V4L2/vb2 kernel code entered by these ioctls executes in the calling
librealsense task context, not in another V4L2 kernel thread. With MMAP, the
payload already occupies the shared frame buffer; `DQBUF` transfers ownership
from the done queue rather than copying the complete frame into userspace.

## 4. Execution Entities in the Normal Path

| Stage | Entity or context | Count/lifetime | Default scheduling | Work | Safe per-camera control? |
|---|---|---|---|---|---|
| DMA | xHCI hardware DMA | Continuous per controller/endpoint | Not applicable | Write payload to host memory | No CPU task exists |
| xHCI event | Hard-IRQ context | Per controller | Not applicable | Process event ring | No |
| xHCI event | `irq/<N>-xhci-hc` | One per IRQ under threadirqs/RT | Commonly FIFO on RT; measure it | Threaded xHCI handler | Yes; experiments used FIFO 90 |
| USB giveback | Current HI_SOFTIRQ context | Per CPU and event | No independent policy | `usb_giveback_urb_bh` | No |
| Deferred softirq | `ksoftirqd/<CPU>` | Persistent per CPU | General kernel task; RT-specific behavior | Execute pending softirq/BH | Not exclusively for a camera |
| UVC payload copy | `kworker/uXX:Y` | Dynamic, shared, migratable | `SCHED_OTHER`, nice -20 | Payload copy, release, URB resubmit | Unsafe to classify by name alone |
| vb2 completion | No independent task | Inline | Inherits copy/BH context | Done list and waiter wakeup | Not applicable |
| V4L2 ioctl | librealsense capture worker | Per active stream/interface | Experimental profile | `select`, DQBUF, callback, QBUF | Yes; covered by profiles |

This list covers direct execution entities in the normal per-frame receive
path. RCU, migration, filesystem, and other generic kernel workers can affect
latency but are not fixed causal stages of every USB-to-V4L2 frame.

## 5. Consequences for the Temporal and Resource Model

### 5.1 Separate SDK and Kernel Demand

The current worker-family utilization accounts for librealsense userspace
workers only. It should be named SDK/userspace CPU utilization rather than
complete acquisition-path utilization.

A complete host decomposition is at least:

```text
U_host = U_xhci + U_usb_bh + U_uvc_copy + U_sdk + U_application
```

`U_xhci`, `U_usb_bh`, and `U_uvc_copy` belong to the kernel receive path.
`U_sdk` is the current LiME/pthread estimate, while `U_application` includes
the wait/consumer and later application processing.

xHCI/USB demand is sporadic and bursty because microframes, URB completions,
and event-ring batching drive it. Long-term average utilization is therefore
insufficient. A later trace should also report:

- total CPU utilization;
- execution-time distribution per burst;
- maximum burst;
- maximum CPU demand in fixed 0.5-ms, 1-ms, 5-ms, and one-frame windows; and
- latency from IRQ entry to `vb2_buffer_done`.

This supports the earlier conclusion that xHCI is poorly represented by a
Deadline reservation based on long-term average bandwidth: short-window burst
demand can be much larger than the average.

### 5.2 Existing Policy Treatments Are Hybrid Configurations

Existing RR/FIFO/Deadline experiments change librealsense worker policies and,
in selected treatments, xHCI IRQ-thread policy. However:

- USB giveback continues to use the kernel's default softirq machinery;
- UVC copy workers remain `SCHED_OTHER`, nice -20; and
- `ksoftirqd/<CPU>` retains the selected kernel's default policy.

The results remain valid system measurements, but the treatment should be
described precisely as:

> role-aware SDK scheduling plus explicit xHCI IRQ scheduling over the Linux
> default USB BH and UVC workqueue implementation.

It is not a uniform scheduling policy over the entire kernel and SDK path.

## 6. Implications for Existing Experiments

### 6.1 Do Not Repeat the Complete Matrix Automatically

Discovering these contexts does not invalidate the main matrix. BH,
`ksoftirqd`, and UVC-workqueue defaults remain constant across treatments on a
given kernel, so the runs still describe the tested system. The causal
language and treatment names require correction:

- a kernel predecessor is not only the xHCI IRQ;
- coordinated kernel scheduling cannot mean xHCI alone; and
- the current CPU model is not complete host-path CPU utilization.

### 6.2 Standard-Kernel Joint Discovery Is Complete

The August 17 trace simultaneously recorded:

- `irq_handler_entry/exit` and `softirq_raise/entry/exit`;
- `workqueue_queue_work` and `workqueue_execute_start/end`;
- `sched_wakeup` and `sched_switch`;
- `usb_giveback_urb_bh`, `uvc_video_complete`, and
  `uvc_video_copy_data_work`;
- `uvc_queue_buffer_complete`, `vb2_buffer_done`, and V4L2 QBUF/DQBUF; and
- userspace measurement boundaries and per-stream freshness metadata.

It covered representative 30-FPS and model-supported stress 60-FPS modes for
D435 and D455, plus concurrent representative acquisition through one
SuperSpeed hub/controller. Section 9 reports the results.

The standard-kernel identity conclusions are:

1. With `threadirqs` disabled, xHCI runs in hard-IRQ context.
2. `usb_giveback_urb_bh` runs in HI_SOFTIRQ/BH. Its task name often appears as
   the interrupted `<idle>` task rather than a dedicated thread.
3. Dynamic `kworker/u22:*` tasks execute `uvc_video_copy_data_work`.
4. Both cameras share the same high-priority unbound worker pool rather than
   owning fixed per-camera kernel threads.
5. D435 and D455 use the same receive stages and execution-entity types.

A matching short PREEMPT_RT trace remains useful to identify threaded xHCI IRQ
and RT softirq execution precisely; it does not require repeating the entire
formal matrix.

### 6.3 Conditions That Would Require New Formal Runs

New controlled results are necessary only if discovery shows that default
UVC/BH execution limits a policy, or if the paper claims coordinated
scheduling of the complete kernel receive path. A minimal host-path comparison
would be:

1. all kernel receive contexts at defaults;
2. xHCI IRQ alone at FIFO 90, matching the current treatment; and
3. xHCI IRQ plus a safely controllable UVC/BH implementation.

The third treatment cannot be implemented by applying `chrt` to every
`kworker/u*`, because the pool is dynamic and shared. A safe research design
might introduce a dedicated kthread worker for UVC copying or an explicit
affinity/policy mechanism in the kernel UVC workqueue. That is a new kernel
modification and needs separate overhead and fairness validation.

Without that implementation, the defensible statement is that Linux's default
BH/UVC workqueue is fixed receive-path infrastructure, while this work
explicitly controls xHCI IRQ and SDK workers and reports that boundary.

### 6.4 Three-Configuration Kernel Comparison

The three xHCI execution configurations must be distinguished explicitly if a
future experiment aims to attribute an application-level latency change to IRQ
threading or to PREEMPT_RT:

| Configuration | Kernel and boot mode | xHCI execution context | Can the xHCI handler receive a POSIX scheduling policy? |
|---|---|---|---|
| A | Standard `CONFIG_PREEMPT`, without `threadirqs` | Hard-IRQ context | No; there is no independently schedulable xHCI task |
| B | Standard `CONFIG_PREEMPT`, with `threadirqs` | `irq/<N>-xhci-hc` thread | Yes; the IRQ thread can receive an explicit policy and priority |
| C | `CONFIG_PREEMPT_RT` | Normally an `irq/<N>-xhci-hc` thread | Yes; IRQ threading is part of the RT execution model |

The differences have distinct interpretations:

- A versus B measures the bundled effect of forcing IRQ threading on the
  standard kernel and making the xHCI handler priority-controllable. It does
  not measure the PREEMPT_RT patch.
- B versus C measures the incremental effect of PREEMPT_RT after xHCI service
  is already threaded. This difference also includes PREEMPT_RT changes to
  kernel preemption, locking, priority inheritance, and softirq behavior.
- A versus C is a deployment-level comparison between two complete kernel
  configurations. It conflates IRQ threading with the other PREEMPT_RT
  mechanisms and cannot attribute a result to either component alone.

Only default-policy or otherwise execution-context-neutral treatments can be
compared across all three configurations. An explicit xHCI RR or FIFO
treatment is defined only for B and C. Configuration A has no xHCI task to
which `chrt` can apply, and hard-IRQ execution must not be labeled as
`SCHED_OTHER` because those mechanisms are not equivalent.

A controlled three-configuration experiment should hold the kernel version,
hardware, camera and USB topology, CPU frequency, UVC request pool, userspace
binary, stream workload, affinity, and injected interference fixed. It should
report both internal receive-path metrics---such as IRQ entry-to-thread-run,
IRQ-to-URB-giveback, and SDK ready-to-running latency---and application-level
inter-delivery and freshness metrics. Signed paired differences are required
to claim an improvement; a mean absolute p99 difference reports only the size
of a difference, not which configuration is faster.

The existing P1 standard-kernel cells belong to configuration B: the standard
kernel was deliberately booted with `threadirqs` so that the xHCI policy factor
was operational. The existing matched P1 kernel result therefore compares B
with C, not default hard-IRQ Linux with PREEMPT_RT. Its small application-level
p99 difference does not establish that PREEMPT_RT is required or that it
uniformly reduces frame-delivery latency. It instead shows that, once xHCI
service is threaded, the observed gain is primarily associated with coordinated
SDK/IRQ treatment rather than with PREEMPT_RT alone.

This three-way experiment is unnecessary if the paper treats PREEMPT_RT as a
chosen execution substrate rather than as an experimental factor. In that
case, the defensible rationale is that PREEMPT_RT provides schedulable,
priority-controllable IRQ service by design; it is not that PREEMPT_RT has
already been shown to lower end-to-end camera latency in every condition.

## 7. Evidence Status

| Finding | Status | Evidence |
|---|---|---|
| xHCI uses `HCD_BH` | Source-confirmed | `xhci.c:5482` |
| Isochronous URBs use the high-priority BH | Source-confirmed | `hcd.c:1734-1747` |
| BH callback invokes `uvc_video_complete` | Source-confirmed | `hcd.c:1646` and UVC callback setup |
| UVC header/FID/EOF parsing is in the BH stage | Source-confirmed | `uvc_video.c:1743-1764` |
| Payload copying uses UVC async workqueue | Source-confirmed | `uvc_video.c:1302-1320` |
| UVC workqueue is unbound and high priority | Source-confirmed | `uvc_driver.c:224-226` |
| High-priority kworker is nice -20, not an RT policy | Source and trace confirmed | `workqueue.c:127`; trace priority 100 |
| Video `vb2_buffer_done` mainly runs in `kworker/u27:*` | Trace-confirmed | 10-minute UVC16 freshness trace |
| Metadata completion can occur in BH/current context | Trace-confirmed | type-13 `<idle>-0` in the same trace |
| Exact PREEMPT_RT task for every BH | Pending joint trace | Existing RT trace lacks softirq/workqueue identity |
| Standard-kernel `kworker/u22:*` executes UVC copy | Joint-trace confirmed | Workqueue and UVC kprobe correlation |
| Standard-kernel stage wall-time distribution | Joint-trace confirmed | 30-s D435/D455 single and concurrent traces |
| Complete kernel CPU demand and burst distribution | Pending | kretprobe duration includes preemption/waiting, not pure execution time |

## 8. Recommendations

1. Describe xHCI IRQ as an important kernel predecessor, not the only kernel
   receive thread.
2. Label current utilization as SDK/userspace demand and separately model
   kernel receive demand.
3. Retain the main experiment matrix rather than repeating it blindly.
4. Preserve the completed standard-kernel trace; add only a matched short RT
   trace if the paper compares implementation identities.
5. Do not repeat the full matrix because of D435/D455 model differences. A
   claim about complete kernel-path scheduling instead requires a controllable
   UVC-worker mechanism and independent validation.

## 9. D435/D455 30-Second Joint Discovery Trace (2026-08-17)

### 9.1 Platform and Topology

The trace ran on Raspberry Pi 5 kernel
`6.12.96-rpi5-standard-btf-uvc16+`, a standard `PREEMPT` kernel rather than
`PREEMPT_RT`. A D455F and D435 shared a 5-Gbit/s SuperSpeed hub `05e3:0626`
and one RP1 xHCI controller:

| Model | Librealsense serial | USB PID | USB topology | Firmware |
|---|---:|---:|---|---|
| D455F | `311322302503` | `8086:0b5c` | `5-1.2`, 5000M | `5.15.0.2` |
| D435 | `948122073863` | `8086:0b07` | `5-1.3`, 5000M | `5.17.3.10` |

Both devices had `power/control=on`, disabling USB autosuspend. Each formal
measurement followed a full reset and used 300 representative warmup frames
or 600 stress warmup frames. A D455 showed a duplicate/out-of-order startup
transient with shorter warmup. It disappeared after 300 frames, so only the
stable measurement window contributes to the results.

### 9.2 Workloads and Freshness

The representative workload used Depth 848x480 Z16 at 30 FPS and Color
640x480 RGB8 at 30 FPS on both models. Stress used 60 FPS with Depth, IR1,
IR2, and Color. D435 Color was 960x540 RGB8; D455 Color was its supported
848x480 RGB8. Each row is one 30-s diagnostic trace, not a repeated WCET
experiment.

| Run | Kernel UVC contexts | Deliveries/camera | xHCI p99 | USB giveback p99 | UVC complete p99 | UVC copy p99 / max | Duplicate / gap / out-of-order |
|---|---:|---:|---:|---:|---:|---:|---:|
| D435 representative | 2 | 899 | 12 us | 16 us | 14 us | 35 / 127 us | 0 / 0 / 0 |
| D455 representative | 2 | 899 | 11 us | 16 us | 13 us | 32 / 499 us | 0 / 0 / 0 |
| D435 stress | 3 | 1,785 | 15 us | 20 us | 17 us | 70 / 1,037 us | 0 / 0 / 0 |
| D455 stress | 3 | 1,789 | 16 us | 24 us | 20 us | 91 / 1,221 us | 0 / 0 / 0 |
| D435 + D455 representative | 4 total | 899 each | 14 us | 16 us | 13 us | 46 / 1,106 us | 0 / 0 / 0 for both |

`Kernel UVC contexts` counts distinct `struct uvc_streaming *` values, not
application-visible logical streams. Depth, IR1, and IR2 share a stereo sensor
path, so the stress configuration exposes four logical streams but three UVC
streaming contexts. An URB callback count is not a frame count because many
URB payloads compose one frame.

Stage duration is kprobe-entry to kretprobe-return wall time. It can include
preemption and nested interrupts, so it is neither LiME CPU execution time nor
a worst-case bound. It compares stages and workload growth but must not be
used directly as Deadline runtime.

### 9.3 Observed Execution Contexts

The joint trace provides direct runtime evidence:

1. **xHCI:** no `irq/137-xhci-hc` task existed on this standard kernel;
   `xhci_irq` ran in hard-IRQ context.
2. **USB giveback:** of 39,648 D435 representative givebacks, 38,964 appeared
   in `<idle>` context. For D455, 38,327 of 39,681 did so. HI_SOFTIRQ borrowed
   the interrupted task identity; the idle task did not initiate camera work.
   Only a small portion ran in `ksoftirqd/0`.
3. **UVC copy:** every `uvc_video_copy_data_work` activation ran in a
   `kworker/u22:*`. Task samples showed `SCHED_OTHER`, nice -20, rather than
   FIFO, RR, or Deadline.
4. **V4L2 completion:** the same worker executing the final UVC copy called
   video `vb2_buffer_done`; no separate vb2 completion thread exists.
5. **Sharing:** in the two-camera run, four UVC contexts distributed work over
   `kworker/u22:0`, `:2`, `:3`, and `:4`. This is a dynamic shared pool, not a
   fixed pool assigned per camera or model.

### 9.4 Cross-Model Result

The D435 and D455 used the same receive architecture and entity types:

```text
xHCI hard IRQ
  -> HI_SOFTIRQ USB giveback
  -> uvc_video_complete
  -> shared high-priority unbound UVC kworker
  -> vb2_buffer_done
  -> librealsense userspace capture worker
```

The model and profiles change UVC-context count, URB/copy activation rate, and
stage-duration distribution, but not the kinds of kernel entities. Sharing a
hub/controller did not create a new per-camera kernel thread. Multi-camera
modeling must therefore separate shared kernel-server burst demand from
per-camera SDK-worker multiplicity rather than copying a complete set of
kernel threads for every device.

### 9.5 Data and Next Step

The laptop backup is:

```text
results/kernel_receive_discovery_20260817/
```

Each run retains `kernel_trace.dat`, `kernel_trace_tasks.csv`,
`v4l2_trace.bin`, `frame_events.csv`, `summary.json`, and
`receive_path_analysis.json`. Raw traces total approximately 70 MiB.

These data support the standard-kernel execution-entity diagram and the D435/
D455 structural equivalence claim. If the paper needs an implementation
comparison, repeat only the same 30-s trace on
`6.12.96-rpi5-rt-btf-uvc16+` to identify threaded xHCI IRQ, RT softirq, and
UVC workqueue contexts. The existing formal policy/interference matrix does
not need repetition for that purpose.
