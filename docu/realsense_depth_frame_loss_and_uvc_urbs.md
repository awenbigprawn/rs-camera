# RealSense D435 Depth-Frame Loss and the `UVC_URBS=16` Mitigation

## 1. Purpose and Conclusions

This document records the investigation of depth timeouts, incomplete frames,
duplicate framesets, and frame-number gaps on a Raspberry Pi 5 with two Intel
RealSense D435 cameras, librealsense, V4L2, and Linux `uvcvideo`. It also
documents the mitigation that increases the Linux UVC isochronous URB pool
from five requests to sixteen.

The evidence supports the following bounded conclusions:

1. The initial `Frame didn't arrive within 15000` failures primarily appeared
   as a failure to deliver depth data on time, rather than a complete inability
   to enumerate the USB device.
2. Direct V4L2 acquisition of the Z16 depth stream succeeded, showing that the
   depth endpoint and host V4L2 path were not permanently broken. The failure
   depended on state and timing.
3. Later kernel/V4L2 traces are consistent with a depth UVC payload that was
   incomplete at a frame boundary. This can explain an incomplete depth frame,
   a sequence gap, and reuse of an older frameset by the upper layer. The trace
   alone cannot uniquely locate the loss at the camera, physical link, or host
   receive path.
4. The official librealsense repository provides a Linux kernel patch that
   changes `UVC_URBS` from 5 to 16. We ported that change to Raspberry Pi Linux
   6.12.96 and validated a separately named kernel with two D435 cameras under
   long 30- and 60-FPS runs.
5. The tested `UVC_URBS=16` PREEMPT_RT configuration produced no duplicates,
   sequence gaps, stale framesets, timeouts, or drops. It also passed the
   steady-state `SCHED_DEADLINE` runtime gate.
6. These results show that UVC16 works well on the tested platform and
   workloads. They do not by themselves prove that every improvement was
   caused by the 5-to-16 change. A causal comparison requires a paired A/B
   experiment with identical hardware, kernel options, policies, and run order.

## 2. Software and Data Path

The paper experiments use this path:

```text
D435 sensor
    -> USB 3.x isochronous endpoint
    -> Raspberry Pi xHCI host controller
    -> Linux uvcvideo driver and URB pool
    -> V4L2 video buffers
    -> librealsense V4L2 backend
    -> librealsense frame synchronization
    -> realsense_steady_probe / application
```

`UVC_URBS` belongs to the kernel `uvcvideo` driver. It is neither the
librealsense frameset-queue depth nor the camera's internal buffer depth.

## 3. Failure and Investigation

### 3.1 Initial Failure

The benchmark initially reported:

```text
RealSense error: Frame didn't arrive within 15000
```

After one occurrence, subsequent runs often failed repeatedly. Recovery tests
included:

- resetting the complete RealSense USB device with `usbreset`;
- toggling the USB device's sysfs `authorized` attribute;
- invoking the librealsense firmware hardware reset;
- waiting for re-enumeration before restarting the pipeline; and
- briefly starting only the depth V4L2 stream as a depth-prime operation.

A USB reset or authorization toggle alone did not recover the device
reliably. Depth-prime sometimes recovered the next pipeline start, but long
campaigns could still enter a sequence of failures. The startup and steady
benchmarks therefore implement full-reset recovery: after a failed logical
run, reset firmware, reset the composite USB device, wait for enumeration,
and retry only that logical run.

### 3.2 Confirming the Depth Path

Depth was the first stream to stop in RealSense Viewer. We then tested its V4L2
node directly:

```sh
timeout 20 v4l2-ctl \
    --device /dev/video4 \
    --set-fmt-video=width=848,height=480,pixelformat=0x2036315a \
    --set-parm=30 \
    --stream-mmap=4 \
    --stream-count=10 \
    --stream-to=/dev/null \
    --verbose
```

`0x2036315a` is the V4L2 FourCC `Z16 `, including the trailing space. This
test delivered ten frames at approximately 33.3-ms intervals. A subsequent
librealsense pipeline start also succeeded in this state. The evidence argues
against permanent failure of the depth sensor and instead points to receive
state, stream restart, or host timing.

### 3.3 Extending Detection from Timeout to Freshness

Counting only successful `wait_for_frames()` calls can hide a failure because
librealsense may return a frameset containing an older depth frame when no
complete new depth frame is available. The steady probe therefore records:

| Metric | Meaning |
|---|---|
| `observed_frames` | Every stream-frame record returned to the application |
| `unique_frames` | New frames identified by stream, frame number, and timestamp |
| `duplicate_frames` | Repeated return of the same frame |
| `sequence_gaps` | Missing frame numbers in a stream sequence |
| `stale_frameset` | A returned frameset in which at least one required stream did not advance |
| `timeouts` / `drops` | Wait timeout or a frame loss explicitly identified by the probe |

These metrics separate a successful API return from delivery of fresh data on
every required stream.

### 3.4 UVC/V4L2 Diagnosis

Near one anomalous depth sequence, targeted tracing was consistent with:

```text
depth payload received by UVC is shorter than a complete frame
    -> V4L2/librealsense rejects it as incomplete or cannot form a new frame
    -> the later frame-number sequence contains a gap
    -> the synchronizer may return a frameset with the previous depth frame
```

The librealsense 2.58.3 V4L2 backend compares `buf.bytesused` with the expected
buffer length and reports an undersized uncompressed buffer as an
`Incomplete video frame`. Duplicate framesets and sequence gaps therefore do
not imply that an application worker simply failed to update memory. They can
result when the lower layer does not deliver an acceptable new depth frame.

An incomplete payload establishes only that the complete data did not reach
the final V4L2 frame buffer. It does not distinguish among:

- incomplete transmission by the camera;
- a cable, connector, power, or signal-integrity problem;
- a missed xHCI/USB isochronous service interval;
- host completion, copy, or URB-resubmission delay that temporarily empties
  the receive queue; and
- abnormal UVC header or boundary processing.

## 4. URBs and `UVC_URBS`

A USB Request Block (URB) is the Linux USB host stack's basic transfer request.
For isochronous UVC capture, `uvcvideo` allocates and submits a pool of URBs:

1. xHCI uses DMA to place camera data into a submitted URB's buffers during
   future USB service intervals.
2. On completion, the UVC path parses packets and payloads.
3. The path may copy payload asynchronously into a V4L2 video buffer.
4. The URB is resubmitted to receive later data.

Linux host-side documentation recommends queuing multiple URBs in advance for
a continuous isochronous stream and avoiding an empty request queue. This
hides interrupt and software-processing latency. Isochronous transfer reserves
scheduled service but does not indefinitely retransmit missed data like a bulk
transfer, so receive-request continuity is particularly important.

Linux 6.12 defines:

```c
/* Number of isochronous URBs. */
#define UVC_URBS        5
/* Maximum number of packets per URB. */
#define UVC_MAX_PACKETS 32
```

`UVC_URBS` is the pool depth for each UVC streaming interface. On a D435, the
depth/stereo and color paths occupy different UVC interfaces, so each active
interface maintains its own pool. A log identifier such as `3-1:1.1` names a
USB device and interface, not URB number one.

## 5. Why Sixteen URBs Can Help

### 5.1 Mechanism

With five requests, a short delay in CPU or IRQ service, completion handling,
asynchronous copying, memory service, workqueue execution, or resubmission can
allow xHCI to complete all submitted URBs before software replenishes them. A
shallow pool reaches this condition more readily.

With sixteen requests:

- xHCI has more already-submitted receive work;
- other URBs can cover later service intervals while one request is parsed,
  copied, or resubmitted late;
- the driver tolerates a longer short-term CPU, IRQ, or memory delay; and
- incomplete UVC payloads caused by host request starvation may become less
  likely.

This is a deeper pre-submitted receive queue. It does not cache sixteen
complete frames and does not intentionally add sixteen frames of latency.

For intuition only, if one URB described 32 SuperSpeed service intervals of
125 us, it would describe about 4 ms; five and sixteen URBs would then
represent roughly 20 and 64 ms. Actual coverage depends on endpoint
descriptors, bursts, packet size, payload segmentation, and driver behavior.
The paper must not present this illustration as a measured D435 buffer time.

### 5.2 Limits

Increasing `UVC_URBS` does not:

- increase physical USB bandwidth;
- repair a bad cable, insufficient power, or signal errors;
- make isochronous USB retransmit missed data;
- correct a camera firmware payload shortage;
- replace suitable IRQ service, CPU scheduling, or memory-bandwidth control; or
- avoid the additional kernel DMA/buffer resources and in-flight requests.

It is a host receive-pipeline margin, not a universal repair for depth-frame
loss.

## 6. Source of the Method

The value 16 comes from the official librealsense repository rather than an
arbitrary local choice. The upstream patch records:

```text
commit:  6640bf79fbce00f056de09d065b2ee34556f04cd
author:  Evgeni Raikhel <evgeni.raikhel@intel.com>
date:    2020-12-01
subject: [PATCH] Change UVC_URBS 5->16
change:  #define UVC_URBS 5  ->  16
```

Official sources:

- [librealsense `uvcvideo_increase_UVC_URBS.patch`](https://github.com/realsenseai/librealsense/blob/master/scripts/uvcvideo_increase_UVC_URBS.patch)
- [librealsense Ubuntu LTS patch script](https://github.com/realsenseai/librealsense/blob/master/scripts/patch-realsense-ubuntu-lts.sh)
- [librealsense Ubuntu LTS HWE patch script](https://github.com/realsenseai/librealsense/blob/master/scripts/patch-realsense-ubuntu-lts-hwe.sh)

Both official installation scripts contain an `Increase UVC_URBs in
uvcvideo` step. This establishes 5-to-16 as an existing librealsense Linux
integration measure, not a guarantee for every kernel, camera, and topology.
It must still be validated on the target platform.

## 7. Local Implementation

The Raspberry Pi Linux 6.12.96 adaptation is archived at:

```text
tools/rpi_kernel_patch/uvcvideo-increase-urbs-16.patch
```

It changes only:

```diff
-#define UVC_URBS        5
+#define UVC_URBS        16
```

Apply it with:

```sh
cd /path/to/rpi-linux-6.12

PATCH_FILE=/home/safebot/program/rs-camera/tools/rpi_kernel_patch/uvcvideo-increase-urbs-16.patch
git apply --check "$PATCH_FILE"
git apply "$PATCH_FILE"

grep '^#define UVC_URBS' drivers/media/usb/uvc/uvcvideo.h
```

Rebuild and install the kernel, modules, and device trees. Use a distinct
`LOCALVERSION` so that the unmodified baseline remains available for A/B
tests. The validation kernel was:

```text
6.12.96-rpi5-rt-btf-uvc16+
```

Its relevant options include PREEMPT_RT, HZ=1000, BTF, and `UVC_URBS=16`.

## 8. UVC16 Validation

### 8.1 Platform Controls

- Raspberry Pi 5;
- two D435 cameras on independent SuperSpeed xHCI controllers;
- V4L2 plus Linux `uvcvideo`;
- CPU frequency fixed at 1500 MHz;
- xHCI IRQ threads assigned to CPU0;
- benchmark tasks assigned to CPU1--3;
- USB autosuspend disabled;
- three 600-s `SCHED_OTHER` calibration traces per workload; and
- validation with generated and corrected `SCHED_DEADLINE` profiles.

### 8.2 `SCHED_OTHER` Calibration

| Workload | Runs | Observed/unique per run | Duplicate / gap / stale / timeout / drop | UVC resubmit errors |
|---|---:|---:|---:|---:|
| Representative: Depth + Color at 30 FPS | 3 x 600 s | 71,946 | All zero | 0 |
| Stress: Depth + IR1 + IR2 + Color at 60 FPS | 3 x 600 s | 285,732--285,740 | All zero | 0 |

### 8.3 Formal `SCHED_DEADLINE` Validation

The three representative 30-FPS runs produced 71,950, 71,946, and 71,944
unique stream-frame records. Every freshness/error metric was zero, and all 21
steady workers received their profile entries.

The corrected stress 60-FPS runs produced:

| Run | Observed | Unique | Duplicate | Sequence gap | Stale | Timeout/drop | UVC warning |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 285,732 | 285,732 | 0 | 0 | 0 | 0 / 0 | 0 |
| 2 | 285,736 | 285,736 | 0 | 0 | 0 | 0 / 0 | 1 |
| 3 | 285,732 | 285,732 | 0 | 0 | 0 | 0 / 0 | 0 |

All 23 steady workers received their profile, and no run invoked full-reset
recovery. The Deadline gate counts exhaustion and SIGXCPU only between
`steady_state_begin` and `steady_state_end`. The corrected 30- and 60-FPS
10-minute validations recorded none. Exhaustion during stop and destruction
was outside the gate.

The full summary is:

```text
results/rt_uvc16/UVC16_RT_VALIDATION_SUMMARY.md
```

## 9. Interpreting `Failed to resubmit video URB (-1)`

One 60-FPS run logged:

```text
uvcvideo 3-1:1.1: Failed to resubmit video URB (-1)
```

- `3-1:1.1` identifies the USB device/interface, which was the depth/stereo
  interface in this topology.
- `-1` is Linux `-EPERM`.
- Linux USB documentation says `usb_submit_urb()` returns `-EPERM` when the
  URB's `reject` flag is set.
- On stream stop, `uvcvideo` poisons URBs so that queued asynchronous work
  cannot resubmit them. Interleaving asynchronous resubmission with stop/poison
  can produce this warning.

The warning is not the loss of one USB packet and is not evidence by itself
that five URBs were insufficient. It is consistent with a rejected-URB race
during teardown. The affected run had no duplicate, gap, stale, timeout, or
drop.

The run did not explicitly correlate kernel printk timestamps with the
probe's `CLOCK_BOOTTIME` markers, so numeric timestamps alone cannot place the
warning precisely inside the steady window. It is described only as captured
near the end of the run.

References:

- [Linux USB error codes](https://docs.kernel.org/driver-api/usb/error-codes.html)
- [Linux USB Request Blocks](https://docs.kernel.org/driver-api/usb/URB.html)
- [Linux USB host-side API](https://docs.kernel.org/driver-api/usb/usb.html)
- [Linux 6.12 `uvc_video.c`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/media/usb/uvc/uvc_video.c?h=v6.12)

## 10. Safe and Unsafe Claims

Supported claims:

- Librealsense provides an official patch that increases the Linux
  `uvcvideo` isochronous URB pool from 5 to 16.
- Under the specified Raspberry Pi 5, two-D435, topology, kernel, and workload
  conditions, long 30- and 60-FPS UVC16 experiments showed no frame-freshness
  failure.
- A deeper pre-submitted URB queue provides more margin for host completion
  and resubmission delays in continuous isochronous capture, consistent with
  the Linux USB documentation.
- `SCHED_DEADLINE` tightened interarrival tails in the same UVC16
  configuration without changing the mean delivery period.

Unsupported claims without a matched A/B experiment:

- every D435 depth-frame loss is caused by `UVC_URBS=5`;
- sixteen URBs guarantee lossless acquisition;
- the one resubmit `-1` warning proves insufficient receive buffering; or
- the existing data strictly prove that sixteen is always better than five.

A paired experiment should alternate five- and sixteen-URB kernels on the same
day while holding camera, port, cable, CPU frequency, IRQ configuration,
kernel options, policy, workload, interference, duration, and probe version
constant. Randomizing or interleaving the order reduces temperature, device
state, and time-drift bias.

## 11. Sources and Local Artifacts

Primary online sources:

1. [Official librealsense UVC URB 5-to-16 patch](https://github.com/realsenseai/librealsense/blob/master/scripts/uvcvideo_increase_UVC_URBS.patch)
2. [Official librealsense Ubuntu LTS patch script](https://github.com/realsenseai/librealsense/blob/master/scripts/patch-realsense-ubuntu-lts.sh)
3. [Official librealsense Ubuntu LTS HWE patch script](https://github.com/realsenseai/librealsense/blob/master/scripts/patch-realsense-ubuntu-lts-hwe.sh)
4. [Linux kernel USB host-side API](https://docs.kernel.org/driver-api/usb/usb.html)
5. [Linux kernel URB documentation](https://docs.kernel.org/driver-api/usb/URB.html)
6. [Linux kernel USB error codes](https://docs.kernel.org/driver-api/usb/error-codes.html)
7. [Linux 6.12 `uvcvideo.h`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/media/usb/uvc/uvcvideo.h?h=v6.12)
8. [Linux 6.12 `uvc_video.c`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/media/usb/uvc/uvc_video.c?h=v6.12)
9. [librealsense 2.58.3 V4L2 backend](https://github.com/realsenseai/librealsense/blob/v2.58.3/src/linux/backend-v4l2.cpp)

Local material:

- `tools/rpi_kernel_patch/uvcvideo-increase-urbs-16.patch`
- `tools/rpi_kernel_patch/README.md`
- `results/rt_uvc16/UVC16_RT_VALIDATION_SUMMARY.md`
- `tools/realsense_steady_bench/parse_freshness_kernel_trace.py`
- `tools/realsense_steady_bench/record_freshness_kernel_trace.sh`

This document describes the experiment state as of 2026-08-08. Update the
validation and causal-claim sections after a strict `UVC_URBS=5` versus 16
paired A/B experiment.
