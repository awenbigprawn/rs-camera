# RealSense Steady-State Benchmark

This benchmark characterizes librealsense after camera startup has completed. It
replaces the former `realsense_usb_topology_bench`, whose `camera_count` field
was only a label and whose probe could operate only one pipeline.

The new probe creates one `rs2::pipeline` per selected camera, waits until every
camera has completed its warm-up, emits one global `steady_state_begin` marker,
and then records the same number of frame deliveries from every camera.

## Campaign factors and fixed controls

| Role | Setting |
| --- | --- |
| Cartesian factors | workload case, scheduling policy, CPU-noise mode, memory-noise mode, GPU-noise mode, and USB-storage-noise mode |
| Operational inputs | camera serials, CPU/memory-noise worker counts and affinity, memory buffer size, frame override, repetitions, build jobs, and output path |
| Fixed controls | V4L2 backend, `uvcvideo`, 1500 MHz CPU, cache drop before each run, and RT priority 80 |

Fixed controls are declared in `steady_settings.py` and are copied into the
Benchkit CSV and per-run manifest. They are intentionally not command-line
factors.

## Python module layout

The campaign runner is split by responsibility:

- `run_steady_campaign.py` parses CLI arguments, selects cases, and constructs
  the Benchkit Cartesian-product campaign;
- `steady_benchmark.py` adapts one steady-state run to Benchkit;
- `steady_attempts.py` owns attempt selection, retry, and artifact promotion;
- `camera_recovery.py` full-resets every selected D435 after a failed attempt;
- `noise_workloads.py` owns CPU, memory, USB-storage, and GPU noise processes;
- `system_controls.py` owns CPU-frequency, backend-binding, topology, and
  kernel-log state;
- `steady_results.py` assembles the per-run Benchkit result row;
- `steady_settings.py` contains shared paths, fixed controls, and factor names.

## Measurements

The probe writes:

- every delivered frame's `CLOCK_BOOTTIME` timestamp;
- the camera, stream, stream index, frame number, sensor timestamp, and
  timestamp domain for each event;
- per-camera and per-stream frame counts and frame-number gaps;
- host and sensor inter-arrival distributions;
- `wait_for_frames()` blocking-time distributions in `wait` mode;
- pipeline start/stop times and the exact global measurement window.

The campaign combines these data with:

- pthread create/start/name/exit/join events from
  `tools/realsense_thread_trace/trace_pthreads.c`;
- scheduler state transitions collected by the unmodified LiME dependency;
- per-thread steady-state execution, ready, sleeping, response, and period
  distributions;
- before/after USB topology, autosuspend state, and xHCI/UVC interrupt
  snapshots;
- the requested Linux scheduling policy and fixed CPU frequency.

Raw frame events and tracing output are retained so paper figures can be
regenerated without rerunning the hardware experiment.

## Probe Modes

`--delivery wait` gives every camera an explicitly named `rs-wait-N` acquisition
thread that calls `wait_for_frames()`. `--delivery callback` lets librealsense
invoke the application callback from its own dispatch path. Both modes use the
same warm-up barrier and measurement interval.

Stream modes are:

- `depth`: depth only;
- `depth_color`: depth and RGB;
- `d435_all`: depth, RGB, infrared 1, and infrared 2.

The default D435 all-stream profile is depth/IR at 848x480 and RGB at 640x480,
all at 30 frames/s.

## Build

From the repository root:

```sh
cmake -S . -B build-realsense-steady -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build-realsense-steady --target realsense_steady_probe -j4
```

To build the selected Vulkan GPU-noise workload, initialize the pinned ncnn
submodule and install the optional dependencies:

```sh
./scripts/install_dependencies_ubuntu.sh --gpu-noise --build
```

## Short Smoke Campaign

The following takes approximately ten seconds per policy after startup:

```sh
sudo -v

.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --case one_camera_all_streams_wait \
  --policies other rr fifo \
  --frames 300 \
  --nb-runs 1 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/smoke
```

The runner locks CPU frequency once before the first run and restores dynamic
scaling after the entire campaign, including interrupted campaigns. The paper
campaign fixes `CAMPAIGN_CPU_FREQUENCY_MHZ = 1500` and
`CAMPAIGN_RT_PRIORITY = 80` near the top of the runner; neither is a Cartesian
factor or CLI override.

Before every attempt, including a retry of the same logical Benchkit run, the
runner executes `sync` and writes `3` to `/proc/sys/vm/drop_caches`. This
establishes a cold Linux page cache and reclaims dentries and inodes before noise
warm-up or camera startup; anonymous memory and swap are not cleared. The
operation is recorded in `memory_cleanup_before_run.json` and the campaign CSV.
Run `sudo -v` before the campaign. Cache cleanup is a fixed paper-campaign
control.

## Multi-camera warm-up, recovery, and retry

The C++ probe uses a global warm-up barrier. It emits `steady_state_begin` and
starts the measured interval only after every selected camera has delivered its
configured number of warm-up frames. For two cameras, one healthy camera cannot
start the measurement while the other reports `Frame didn't arrive`.

Full-reset recovery is enabled by default. If an attempt fails before
`steady_state_begin`, the runner:

1. preserves that failed attempt and its LiME, pthread, topology, kernel-log,
   noise, and probe artifacts under `attempt-N/`;
2. performs a librealsense firmware hardware reset for every selected camera;
3. resets the parent composite USB device for every camera, thereby resetting
   all Depth, RGB, and IR UVC interfaces together;
4. waits for every serial number to re-enumerate; and
5. repeats only the same logical Benchkit run, up to three attempts by default.

The recovery controls are `--recover-on-failure {none,full-reset}`,
`--max-attempts-per-run`, `--recovery-reset-timeout-ms`,
`--recovery-wait-seconds`, and `--recovery-settle-seconds`. A successful
selected attempt is promoted to the normal run directory so existing analysis
continues to find `steady_summary.json`, `frame_events.csv`, and
`lime_trace/`. `attempts.json`, `selected_attempt.txt`, and the Benchkit CSV
record whether success was immediate or followed recovery.

A failure after `steady_state_begin` is a measured steady-state outcome. The
runner resets all cameras to protect the following campaign point but does not
retry and conceal that failure.

Use `--no-lime` only for functional debugging. It keeps the pthread lifecycle
trace but cannot produce scheduler execution-time distributions.

## Long Single-Camera Run

For a ten-minute, 30-frame/s acquisition:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --case one_camera_all_streams_wait \
  --policies other \
  --frames 18000 \
  --nb-runs 5 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/one_camera_10min
```

## Five-Minute Parameter Exploration

`configs/parameter_exploration_5min.json` defines two single-camera workloads:

- representative: depth 848x480 Z16 at 30 frames/s and color 640x480
  RGB8 at 30 frames/s, with infrared disabled;
- stress: depth and both Y8 infrared streams at 848x480, plus color
  960x540 RGB8, all at 60 frames/s.

Each run excludes a ten-second warm-up and measures five minutes. The following
Benchkit Cartesian product runs both workloads under `SCHED_OTHER` and
`SCHED_RR`, with three repetitions per combination:

```sh
sudo -v

.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/parameter_exploration_5min
```

This creates twelve runs:

```text
2 workloads x 2 scheduling policies x 3 repetitions
```

The workload description and exact profiles are copied into `case.json` and
flattened into the Benchkit result CSV.

## Register-Only CPU Busy-Loop Noise

The `busy_loop` condition launches a `SCHED_OTHER` process containing a
configurable number of worker threads. Each worker repeatedly applies dependent
integer operations to a thread-private register value. The hot loop performs no
allocation, array traversal, `memcpy`, file access, system call, or shared-memory
update. It checks one signal flag per 4096 operations and writes its counters
only after termination. This makes the workload primarily compete for CPU
execution time and scheduler service while minimizing cache, memory-bandwidth,
I/O, and USB/DMA effects.

Set the worker count once per campaign with `--cpu-noise-workers N`. Increasing
the count occupies more logical CPUs until the selected affinity mask is
saturated. Counts above the number of available CPUs increase runnable-task
contention but cannot increase physical CPU execution throughput. Use
`--cpu-noise-cpu-affinity` to co-locate the workers with the camera and
librealsense threads.

A Raspberry Pi 5 dose-response pilot can run separate campaigns with 1, 2, 3,
and 4 workers. The formal matrix should then use one selected count rather than
turning worker count into another Cartesian factor. For example, the paired
four-worker experiment is:

```sh
sudo -v
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --cpu-noise-modes none busy_loop \
  --cpu-noise-workers 4 \
  --gpu-noise-modes none \
  --usb-storage-noise-modes none \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/cpu_busy_4workers_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 CPU-noise modes x 3 repetitions
```

The workload warms up for ten seconds before camera startup. Each record reports
aggregate process CPU time, CPU equivalents (`process CPU time / wall time`),
normalized worker utilization, completed register-loop iterations, start/stop
timestamps, worker count, and affinity. Optional timing controls are
`--cpu-noise-warmup-seconds` and `--cpu-noise-ready-timeout-seconds`.

## Fixed-Size Memory-Copy Noise

The `fixed_copy` condition isolates sustained host-memory contention from the
register-only CPU condition. Every `SCHED_OTHER` worker owns two aligned,
pre-touched buffers and alternates their roles as source and destination for
each `memcpy`. The default 64 MiB buffer is deliberately larger than ordinary
CPU caches. With `N` workers, the process allocates `2 x N x 64 MiB` by default.

Set a fixed dose per campaign with `--memory-noise-workers N` and
`--memory-noise-buffer-size-mib M`. More workers can increase memory-controller
pressure until bandwidth saturates, but they also consume CPU execution time;
the reported process CPU equivalents quantify that unavoidable component. Use
`--memory-noise-cpu-affinity` when a controlled CPU placement is required.

For example, compare baseline and four-worker memory contention:

```sh
sudo -v
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --cpu-noise-modes none \
  --memory-noise-modes none fixed_copy \
  --memory-noise-workers 4 \
  --memory-noise-buffer-size-mib 64 \
  --gpu-noise-modes none \
  --usb-storage-noise-modes none \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/memory_copy_4workers_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 memory-noise modes x 3 repetitions
```

The runner waits for ten seconds of copy progress before camera startup. Each
record contains payload-copy MiB/s, an estimated read-plus-write memory traffic
rate, allocated bytes, buffer size, worker count, CPU equivalents, affinity,
and `CLOCK_BOOTTIME` boundaries. The read-plus-write rate is an algorithmic
estimate of two transferred bytes per copied payload byte, not a hardware
memory-controller counter.

## Read-Only USB Storage Noise

The `sequential_read` mode continuously reads an unmounted whole USB disk using
1 MiB `O_DIRECT` requests and wraps at the end of the device. It is deliberately
read-only: camera traffic and storage reads both flow device-to-host, while
writes would add flash wear and garbage-collection variability.

The runner performs safety checks before starting the campaign. The target must
be a whole USB block device, no partition on it may be mounted, and a stable
`/dev/disk/by-id/...` path is strongly recommended. After opening the device
read-only with elevated permission, the workload drops back to the invoking
UID/GID. It runs as `SCHED_OTHER`, reads for ten seconds before signalling ready,
and records achieved throughput and read-latency statistics for every run.

Run a paired five-minute read-noise comparison with GPU noise disabled:

```sh
sudo -v
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --gpu-noise-modes none \
  --usb-storage-noise-modes none sequential_read \
  --usb-storage-device /dev/disk/by-id/usb-MODEL_SERIAL-0:0 \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/usb_read_noise_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 USB-noise modes x 3 repetitions
```

Use `lsusb -t` and the recorded sysfs paths to verify topology. For shared-bus
experiments, use a SuperSpeed storage device on the same powered USB 3 hub as
the camera. A USB 2 device or a device on another xHCI controller does not
create direct SuperSpeed-link contention and must be labelled accordingly.
Optional controls are `--usb-storage-warmup-seconds`,
`--usb-storage-block-size-kib`, and `--usb-storage-ready-timeout-seconds`.

## Selected GPU-Noise Workload

The formal GPU-interference condition is `mobilenet_v2_vulkan`. It continuously
executes the pinned ncnn MobileNetV2 224x224x3 computation graph on a hardware
Vulkan device. `none` is the paired baseline. Synthetic Vulkan arithmetic and
GPU memory-copy loops, ResNet18, and YOLO are intentionally not exposed as formal
benchmark modes.

The graph uses deterministic zero weights and a fixed input tensor. Classification
accuracy is irrelevant to this interference workload; this choice makes every
run independent of external model downloads while preserving the exact
MobileNetV2 layer graph and GPU command stream used during candidate selection.
The ncnn git revision and SHA-256 of `mobilenet_v2.param` identify the workload.

Before each camera run, the runner starts one `SCHED_OTHER` noise process and
waits for model loading plus ten warm-up inferences. Only after
`gpu_noise_ready.json` exists does camera startup begin. At the end of the run,
the process handles `SIGTERM`, completes its in-flight inference, and writes
latency and iteration statistics. A software Vulkan CPU device is rejected, so
llvmpipe cannot silently turn this into CPU noise. On Raspberry Pi, the runner
auto-selects `/usr/share/vulkan/icd.d/broadcom_icd.json` when it exists.

Run a paired five-minute comparison for both workloads and two policies:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --gpu-noise-modes none mobilenet_v2_vulkan \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/gpu_noise_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 GPU-noise modes x 3 repetitions
```

Optional controls include `--gpu-noise-device`,
`--gpu-noise-warmup-iterations`, `--gpu-noise-ready-timeout-seconds`,
`--gpu-noise-vulkan-icd`, and `--gpu-noise-cpu-affinity`. CPU affinity is left
unset by default to match the candidate-selection experiment.

## Multiple Cameras

Pass one serial per camera. Each selected camera gets its own pipeline:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --case one_camera_all_streams_wait \
  --policies other \
  --frames 18000 \
  --nb-runs 5 \
  --serial SERIAL_1 \
  --serial SERIAL_2 \
  --results-dir tools/realsense_steady_bench/results/two_cameras
```

Command-line serials override the case configuration and set `camera_count`
accordingly. Keep physical hub/controller labels in a copied JSON configuration
so that the result CSV identifies the actual setup.

## Retained RSUSB support

RSUSB build and UVC unbind/rebind support remains in the codebase for separate
backend validation. It is not used by the paper campaign, which fixes
`CAMPAIGN_BACKEND = "v4l2"` and uses the kernel `uvcvideo` driver. Any RSUSB
validation must change the explicit constants in the runner and use a separate
build and results directory; backend results must never be mixed in one
campaign.

## Result Layout

Every Benchkit record directory contains:

```text
case.json
run_manifest.json
memory_cleanup_before_run.json
topology_before.json
topology_after.json
steady_summary.json
kernel_log.txt
frame_events.csv
probe_stdout.txt
cpu_noise_configuration.json
cpu_noise_ready.json
cpu_noise_summary.json
cpu_noise_process.json
cpu_noise_stdout.txt
cpu_noise_stderr.txt
memory_noise_configuration.json
memory_noise_ready.json
memory_noise_summary.json
memory_noise_process.json
memory_noise_stdout.txt
memory_noise_stderr.txt
gpu_noise_configuration.json
gpu_noise_ready.json
gpu_noise_summary.json
gpu_noise_process.json
gpu_noise_stdout.txt
gpu_noise_stderr.txt
usb_storage_noise_configuration.json
usb_storage_noise_ready.json
usb_storage_noise_summary.json
usb_storage_noise_process.json
usb_storage_noise_stdout.txt
usb_storage_noise_stderr.txt
thread_lifecycle.jsonl
lime_trace/
thread_steady_intervals.csv
thread_steady_activations.csv
thread_steady_summary.csv
thread_steady_summary.json
```

`frame_events.csv` is the source for frame-loss and inter-arrival analysis.
`thread_steady_activations.csv` groups scheduler fragments into jobs separated
by blocking/sleep intervals; preemption does not incorrectly create a new job.
The summary uses complete jobs when possible and reports partial jobs at the
measurement boundaries separately in the raw activation CSV.

## Paper Metrics

Primary response variables should include:

- frame-number loss rate per camera and stream;
- host inter-arrival p99, p99.9, and maximum;
- sensor versus host inter-arrival divergence;
- acquisition-thread execution, ready, response, and period tails;
- UVC/xHCI interrupt deltas, UVC resubmit errors, and CPU placement.

Startup time is deliberately not mixed into these values: the measurement
window starts only after all cameras have warmed up.
