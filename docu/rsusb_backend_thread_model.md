# RSUSB Backend Thread Model and Comparison with V4L2

## 1. Purpose

This document records how the Linux RSUSB backend changes the userspace thread
model of `librealsense` relative to the native V4L2/`uvcvideo` backend.  The
comparison uses matched two-camera steady-state experiments and source
inspection.  It distinguishes three quantities that must not be conflated:

1. userspace threads alive during the measured steady-state window;
2. all child threads created during startup, steady state, and teardown; and
3. kernel execution contexts in the USB receive path.

The current 21/23-worker timing model used by the paper was derived for V4L2.
The evidence below shows that this model is not backend-independent.

## 2. Compared Experiments

The matched experiments are stored under:

- `results/rpi5/hostpath_factorial_20260810_resume/uvc16_no_isolation/`;
- `results/rpi5/hostpath_factorial_20260810_resume/rsusb_no_isolation/`; and
- `results/rpi5/hostpath_factorial_20260810_resume/rsusb_no_isolation_missing_stress/`.

Both backends used:

- two Intel RealSense D435 cameras, serials `948122073863` and
  `327122075717`;
- `SCHED_OTHER`;
- no CPU, memory, GPU, or USB-storage interference;
- no CPU affinity, taskset, or CPU isolation;
- a fixed CPU frequency of 1.5 GHz;
- a 600-s measurement interval; and
- three repetitions per workload.

The representative workload enabled Depth at 848x480 Z16 and Color at 640x480
RGB8, both at 30 FPS.  The stress workload enabled Depth, IR1, and IR2 at
848x480 and Color at 960x540, all at 60 FPS.  The V4L2 build used `uvcvideo`
with `UVC_URBS=16`.  The RSUSB build unbound `uvcvideo` and used the libusb
receive path.

For each attempt, `thread_lifecycle.jsonl` records pthread creation and exit,
while `thread_steady_summary.json` restricts LiME and lifecycle observations to
the interval delimited by `steady_state_begin` and `steady_state_end`.

## 3. Observed Steady-State Thread Counts

The steady-state count includes the process main thread, application wait
workers, active workers, sleeping workers, and dormant workers whose lifetime
overlaps the measurement interval.

| Workload | V4L2 threads | RSUSB threads | RSUSB increase | V4L2 creator-stack families | RSUSB creator-stack families |
|---|---:|---:|---:|---:|---:|
| Representative, 30 FPS | 22 | 39 | 17 | 12 | 17 |
| Stress, 60 FPS | 24 | 45 | 21 | 12 | 17 |

All three repetitions produced the same steady-state count for their backend
and workload.  Excluding the process main thread gives the counts used by the
worker timing model:

| Workload | V4L2 workers | RSUSB workers |
|---|---:|---:|
| Representative, 30 FPS | 21 | 38 |
| Stress, 60 FPS | 23 | 44 |

The entry-module composition further confirms that the increase occurs in
userspace:

| Workload and backend | Main | Application `rs-wait` | Other C++ workers | `libusb_event` | Total |
|---|---:|---:|---:|---:|---:|
| Representative, V4L2 | 1 | 2 | 19 | 0 | 22 |
| Representative, RSUSB | 1 | 2 | 35 | 1 | 39 |
| Stress, V4L2 | 1 | 2 | 21 | 0 | 24 |
| Stress, RSUSB | 1 | 2 | 41 | 1 | 45 |

Most C++ workers have the same truncated Linux name, `realsense_stead`.
Consequently, the counts above use normalized creator-stack signatures rather
than thread names to distinguish worker families.

## 4. Receive-Path Composition

### 4.1 Components shared by both backends

The high-level SDK structure remains present with either backend.  It includes
pipeline dispatch, synchronization and frameset aggregation, notification
dispatch, timestamp maintenance, firmware-status polling, and one application
wait worker per camera.  These common functions do not imply identical thread
signatures because the backend build and creator path differ.

The Linux xHCI controller and USB core also remain in both paths.  RSUSB is
therefore not a userspace replacement for the complete kernel USB stack.

### 4.2 Native V4L2 backend

The V4L2 userspace path contains:

- a udev-based device watcher; and
- one `v4l_uvc_device::capture_loop()` worker for each active V4L2 receive
  path.

The matched traces contain four capture-path workers in the representative
configuration and six in the stress configuration.  Enabling both IR streams
adds one shared depth/IR receive path per camera rather than one userspace
capture thread for each IR image.

Before the capture worker can dequeue a frame, the kernel performs xHCI event
handling, USB completion handling, UVC payload assembly, and V4L2/vb2 buffer
completion.  Those hard-IRQ, softirq, threaded-IRQ, and kworker contexts are
not pthreads and therefore are not included in the 22/24 userspace counts.

The V4L2 capture worker is created in:

- `deps/librealsense/src/linux/backend-v4l2.cpp:1676`.

Its steady loop waits for V4L2 readiness and performs DQBUF, callback, and
requeue operations in:

- `deps/librealsense/src/linux/backend-v4l2.cpp:1775`; and
- `deps/librealsense/src/linux/backend-v4l2.cpp:1886`.

### 4.3 RSUSB backend

RSUSB bypasses Linux `uvcvideo` and V4L2 for video streaming.  It uses libusb
to submit USB requests and performs the UVC processing path in userspace.  The
source creates the following backend-specific workers:

- one process-wide libusb event handler that repeatedly calls
  `libusb_handle_events_completed()`;
- a polling device watcher that re-enumerates devices every 2 s;
- one or more `rs_uvc_device` action dispatchers for control operations; and
- for each active `uvc_streamer`, an action dispatcher, a watchdog, and a
  publish-frame active object.

The three per-stream worker types are visible in the creator-stack
multiplicities: their total instances increase from four in the representative
workload to six in the stress workload.  This matches the number of active
receive paths across the two cameras.  Fixed-multiplicity RSUSB dispatchers and
the process-wide libusb event thread account for additional userspace workers.

The relevant source locations are:

- libusb event loop:
  `deps/librealsense/src/libusb/context-libusb.cpp:81-98`;
- RSUSB polling watcher:
  `deps/librealsense/src/polling-device-watcher.h:15-68`;
- `rs_uvc_device` action dispatcher:
  `deps/librealsense/src/uvc/uvc-device.cpp:82-100`;
- per-stream action dispatcher:
  `deps/librealsense/src/uvc/uvc-streamer.cpp:18-33`;
- publish-frame active object:
  `deps/librealsense/src/uvc/uvc-streamer.cpp:88-96`;
- watchdog:
  `deps/librealsense/src/uvc/uvc-streamer.cpp:98-111`; and
- request completion, userspace copy, queueing, and resubmission:
  `deps/librealsense/src/uvc/uvc-streamer.cpp:113-139`.

The exact archived AArch64 RSUSB binary is not stored with the result copy.
The report therefore does not assign every offset-only creator signature to a
specific C++ object.  The total counts, entry modules, signature
multiplicities, and source-level thread types are nevertheless directly
observed or source-supported.

## 5. Whole-Process Creation Counts

The lifecycle interposer also records transient threads created outside the
steady-state interval:

| Workload | V4L2 child threads created | RSUSB child threads created |
|---|---:|---:|
| Representative, 30 FPS | 45 in every run | 106--108 |
| Stress, 60 FPS | 47 in every run | 112 in every run |

These counts exclude the process main thread.  They include temporary device
enumeration, configuration, startup, and teardown work and must not be used as
the steady-state task count.  They do show that RSUSB has a substantially more
active userspace lifecycle, especially during discovery and stream setup.

## 6. Modeling and Scheduling Consequences

The following conclusions are supported by the matched results:

1. The current V4L2 21/23-worker model cannot be relabeled as an RSUSB model.
2. Backend-independent pipeline functions can retain their conceptual
   categories, but the receive-path worker families and multiplicities must be
   reconstructed separately.
3. V4L2 transfers part of the per-frame work to kernel `uvcvideo` and related
   execution contexts.  RSUSB transfers UVC request handling, copying,
   watchdog service, and publication into additional userspace workers.
4. Rate-monotonic priorities and `SCHED_DEADLINE` parameters derived from V4L2
   creator signatures cannot be applied directly to RSUSB.  RSUSB requires its
   own traces, family mapping, execution bounds, and activation intervals.
5. Comparing only userspace thread counts understates the V4L2 path because it
   omits xHCI, USB, UVC, and vb2 kernel work.  A backend comparison must report
   both userspace workers and kernel receive-path execution contexts.

For the RTNS paper, the detailed temporal and scheduling model should remain
explicitly scoped to V4L2/`uvcvideo`.  RSUSB can be presented as a distinct
backend comparison, not as a drop-in implementation of the same thread graph.

## 7. Representative Artifacts

Representative per-run evidence is available at:

- V4L2, representative:
  `results/rpi5/hostpath_factorial_20260810_resume/uvc16_no_isolation/benchmark_rpi5-realsense_realsense_steady_20260810_112905_348474/case_id-representative_depth_color_30fps_10min/policy-other/cpu_noise-none/memory_noise-none/gpu_noise-none/usb_storage_noise-none/run-1/attempt-1/thread_steady_summary.json`;
- V4L2, stress:
  `results/rpi5/hostpath_factorial_20260810_resume/uvc16_no_isolation/benchmark_rpi5-realsense_realsense_steady_20260810_112905_348474/case_id-stress_all_streams_60fps_10min/policy-other/cpu_noise-none/memory_noise-none/gpu_noise-none/usb_storage_noise-none/run-1/attempt-1/thread_steady_summary.json`;
- RSUSB, representative:
  `results/rpi5/hostpath_factorial_20260810_resume/rsusb_no_isolation/benchmark_rpi5-realsense_realsense_steady_20260810_123514_647023/case_id-representative_depth_color_30fps_10min/policy-other/cpu_noise-none/memory_noise-none/gpu_noise-none/usb_storage_noise-none/run-1/attempt-1/thread_steady_summary.json`; and
- RSUSB, stress:
  `results/rpi5/hostpath_factorial_20260810_resume/rsusb_no_isolation/benchmark_rpi5-realsense_realsense_steady_20260810_123514_647023/case_id-stress_all_streams_60fps_10min/policy-other/cpu_noise-none/memory_noise-none/gpu_noise-none/usb_storage_noise-none/run-1/attempt-1/thread_steady_summary.json`.
