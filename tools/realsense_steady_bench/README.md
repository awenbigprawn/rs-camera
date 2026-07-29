# RealSense Steady-State Benchmark

This benchmark characterizes librealsense after camera startup has completed. It
replaces the former `realsense_usb_topology_bench`, whose `camera_count` field
was only a label and whose probe could operate only one pipeline.

The new probe creates one `rs2::pipeline` per selected camera, waits until every
camera has completed its warm-up, emits one global `steady_state_begin` marker,
and then records the same number of frame deliveries from every camera.

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
  --priority 80 \
  --frames 300 \
  --nb-runs 1 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/smoke
```

The runner locks CPU frequency once before the first run (1500 MHz by default)
and restores dynamic scaling after the entire campaign, including interrupted
campaigns. Disable locking with `--cpu-frequency-mhz 0`.

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
  --cpu-frequency-mhz 1500 \
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
  --priority 80 \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --cpu-frequency-mhz 1500 \
  --results-dir tools/realsense_steady_bench/results/parameter_exploration_5min
```

This creates twelve runs:

```text
2 workloads x 2 scheduling policies x 3 repetitions
```

The workload description and exact profiles are copied into `case.json` and
flattened into the Benchkit result CSV.

## Selected GPU-Noise Workload

The formal GPU-interference condition is `mobilenet_v2_vulkan`. It continuously
executes the pinned ncnn MobileNetV2 224x224x3 computation graph on a hardware
Vulkan device. `none` is the paired baseline. Synthetic Vulkan arithmetic and
memory-copy loops, ResNet18, and YOLO are intentionally not exposed as formal
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
  --priority 80 \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --cpu-frequency-mhz 1500 \
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

## RSUSB Backend

V4L2 is the default and should be used for comparable laptop/Raspberry Pi
experiments. RSUSB remains available:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --rsusb-backend \
  --rsusb-usb-device 3-1 \
  --case one_camera_all_streams_wait \
  --serial CAMERA_SERIAL
```

For multiple RSUSB cameras, repeat both `--serial` and
`--rsusb-usb-device`. The runner unbinds UVC interfaces once before the
campaign and rebinds them during cleanup.

## Result Layout

Every Benchkit record directory contains:

```text
case.json
run_manifest.json
topology_before.json
topology_after.json
steady_summary.json
kernel_log.txt
frame_events.csv
probe_stdout.txt
gpu_noise_configuration.json
gpu_noise_ready.json
gpu_noise_summary.json
gpu_noise_process.json
gpu_noise_stdout.txt
gpu_noise_stderr.txt
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
