# RealSense D435 Real-Time Benchmark Experiment Design

## 1. Purpose

This document defines the experimental methodology for characterizing the
temporal behavior of Intel RealSense D435 cameras and librealsense on a
Raspberry Pi 5. The study targets:

- librealsense thread creation, lifetime, activation, blocking, and execution;
- startup latency and startup-only threads;
- steady-state frame acquisition and periodic threads;
- frame jitter, deadline misses, timeouts, and frame loss;
- the effects of Linux scheduling policies and `PREEMPT_RT`;
- scalability from one to four cameras;
- sensitivity to CPU and USB I/O interference;
- calibration and evaluation of per-thread `SCHED_DEADLINE` reservations.

The main research questions are:

1. What temporal model describes librealsense startup and steady-state
   execution?
2. Which threads are periodic, sporadic, startup-only, or mostly dormant?
3. How do `PREEMPT_RT` and the Linux scheduling policies affect scheduling
   latency, execution time, frame timing, and frame loss?
4. At what point do CPU, USB, and multi-camera scaling become the dominant
   sources of delay?
5. Can calibrated `SCHED_DEADLINE` reservations improve predictability without
   starving the USB and camera-supporting kernel threads?

## 2. Experimental Platform

| Component | Fixed configuration |
|---|---|
| Computer | Raspberry Pi 5 |
| Operating system | Ubuntu Server 24.04 |
| Kernel source | Linux 6.12 |
| Kernel variants | Default preemption and `PREEMPT_RT` |
| Kernel tracing support | BTF enabled for LiME/eBPF |
| Kernel timer frequency | `CONFIG_HZ_1000=y` |
| CPU frequency | All online cores fixed at 1500 MHz |
| USB backend | librealsense V4L2 backend with `uvcvideo` |
| Camera | Intel RealSense D435 |
| Camera connection | USB 3 SuperSpeed |
| Application architecture | One process, one `rs2::pipeline` per camera |
| Frame delivery API | Blocking `wait_for_frames()` unless explicitly stated |

The two kernels must be built from the same source revision and should differ
only in the configuration changes required by `PREEMPT_RT`. Record for every
campaign:

- `uname -a`;
- kernel git revision and build identifier;
- complete kernel `.config`;
- compiler version;
- kernel command line;
- `CONFIG_PREEMPT`, `CONFIG_PREEMPT_RT`, `CONFIG_HZ`, and BTF settings;
- `/proc/sys/kernel/sched_rt_period_us`;
- `/proc/sys/kernel/sched_rt_runtime_us`;
- the round-robin timeslice;
- active clocksource.

## 3. Independent Variables

### 3.1 Kernel

```text
kernel:
  linux_6_12_default_btf
  linux_6_12_preempt_rt_btf
```

BTF is a tracing requirement rather than an experimental treatment. It must be
enabled in both kernel variants.

### 3.2 Scheduling policy

```text
scheduling_policy:
  SCHED_OTHER
  SCHED_RR
  SCHED_FIFO
  SCHED_DEADLINE
```

Fixed parameters:

```text
SCHED_OTHER:
  nice: 0

SCHED_RR:
  priority: 80
  rr_timeslice: record from the running kernel

SCHED_FIFO:
  priority: 80

SCHED_DEADLINE:
  assignment: per stable librealsense thread
  runtime: calibrated high execution-time bound plus margin
  deadline: thread-specific relative deadline
  period: measured activation period or safe minimum inter-arrival time
```

The effective policy and priority of every traced thread must be recorded.
`SCHED_DEADLINE` is discussed separately in Section 10.

### 3.3 Workload

#### Representative workload

```text
Depth: 848x480, Z16, 30 frames/s
Color: 640x480, RGB8, 30 frames/s
Infrared: disabled
```

This workload represents a common robot perception configuration. The D435
still uses its infrared imagers internally to calculate depth, but the
application does not request the two additional infrared streams over USB.

#### Stress workload

```text
Depth:      848x480, Z16, 60 frames/s
Infrared 1: 848x480, Y8,  60 frames/s
Infrared 2: 848x480, Y8,  60 frames/s
Color:      960x540, RGB8, 60 frames/s
```

This workload maximizes regular full-frame traffic without using the special
cropped 300-frames/s modes. It intentionally increases USB bandwidth, frame
synchronization work, memory traffic, and host-side color conversion.

Requesting `RGB8` may cause librealsense to convert a camera-native USB format
such as YUYV on the host. The requested API format, active librealsense profile,
V4L2 wire format, and conversion threads must therefore be documented.

### 3.4 Number of cameras

```text
camera_count:
  1
  2
  3
  4
```

All cameras in one run use the same workload. Each camera has an independent
pipeline and application wait thread. Camera serial numbers and their mapping
to pipeline indices must be recorded.

### 3.5 Noise

```text
noise:
  none
  cpu_busy
  shi_tomasi
  usb_storage_io
```

An optional memory-bandwidth workload may be added after the primary matrix if
CPU scheduling contention needs to be separated from memory contention.

## 4. USB Topology and Camera Placement

Camera count must not be accidentally confounded with USB placement. The
default balanced placement is:

```text
1 camera:
  hub A, port A1

2 cameras:
  hub A, port A1
  hub B, port B1

3 cameras:
  hub A, ports A1 and A2
  hub B, port B1

4 cameras:
  hub A, ports A1 and A2
  hub B, ports B1 and B2
```

Both hubs must be externally powered. Fix and report:

- hub vendor, model, and power supply;
- Raspberry Pi USB port used by each hub;
- camera-to-hub port mapping;
- USB cables;
- negotiated speed for every camera;
- xHCI controller and root hub;
- USB autosuspend setting;
- `uvcvideo` module parameters.

Save `lsusb -t`, relevant sysfs topology, and `/proc/interrupts` before and
after every run. Powered hubs improve power delivery but do not necessarily
provide independent host controllers. The actual topology must be verified
rather than inferred from the number of physical ports.

If USB topology becomes an explicit factor, evaluate it in a separate focused
experiment:

```text
usb_topology:
  direct
  two_cameras_same_hub
  two_cameras_separate_hubs
  balanced_two_hubs
```

### 4.1 Multi-camera synchronization

The primary scalability experiment uses:

```text
camera_synchronization: independent
```

Independent cameras have unrelated frame phases and may distribute USB load
over time. Hardware-synchronized cameras can create simultaneous bursts and
should be studied later as a separate worst-case condition rather than mixed
into the primary camera-count factor.

## 5. Controlled System Conditions

### 5.1 CPU frequency and thermal conditions

Before the first run in a campaign:

1. save the current CPU frequency policy;
2. set all online cores to 1500 MHz;
3. verify the effective frequency;
4. start temperature and throttling monitoring.

After the campaign, including interrupted campaigns, restore the original
frequency policy.

Record:

- CPU frequency at the beginning and end of every run;
- minimum, mean, and maximum temperature;
- thermal throttling flags;
- cooling hardware and fan policy;
- Raspberry Pi power supply rating and undervoltage flags.

The system should run headless. Disable or document unrelated services,
automatic updates, indexing, and other periodic background tasks. SSH traffic
should be limited during measured intervals.

### 5.2 CPU and IRQ affinity

CPU migration and IRQ placement can dominate scheduling latency. Select one
affinity plan during pilot experiments and keep it fixed for the main study.
A recommended controlled layout is:

```text
CPU 0:
  housekeeping, SSH, and fixed xHCI IRQ processing

CPU 1-3:
  camera application and librealsense threads
```

CPU-noise workers should be co-located with the camera threads when measuring
direct CPU contention. Record the effective affinity of:

- every librealsense and application thread;
- xHCI and USB IRQ threads;
- noise workers;
- relevant kernel worker threads.

On `PREEMPT_RT`, most interrupts execute as schedulable IRQ threads. A
high-priority camera thread can starve a lower-priority xHCI IRQ thread,
especially under `SCHED_FIFO`. IRQ policy and priority must therefore be
recorded and considered part of the scheduling model.

### 5.3 Camera controls and physical scene

Fix the following across runs:

- firmware version;
- exposure and gain policy;
- auto-exposure priority;
- emitter enabled state;
- laser power;
- camera orientation;
- scene geometry;
- lighting intensity and flicker;
- camera temperature preconditioning.

For maximum reproducibility, allow auto exposure to settle during
preconditioning, record the resulting exposure and gain, and then use fixed
values during measured runs. If auto exposure remains enabled, keep the scene
and lighting fixed and disable any option that permits the camera to lower its
frame rate to obtain a longer exposure.

Before the first campaign of a session, stream the selected workload for five
minutes to reduce cold-camera and thermal transients.

## 6. Experiment Phases

### 6.1 Parameter exploration

Purpose: validate profiles, timeouts, warm-up length, tracing volume, and
measurement stability before collecting paper data.

```text
warm-up per run: 10 s, excluded
measurement per run: 5 min
repetitions: 3
workloads: representative and stress
policies: SCHED_OTHER and SCHED_RR
camera count: 1
noise: none
```

The current Benchkit configuration is:

```text
tools/realsense_steady_bench/configs/parameter_exploration_5min.json
```

### 6.2 Startup phase

Each startup attempt should always collect ten framesets. Record intermediate
timestamps rather than varying the stopping point from one to ten frames.

```text
startup repetitions per condition: 20
framesets per startup: 10
```

Record:

1. context construction begin/end;
2. device enumeration;
3. pipeline construction begin/end;
4. pipeline start begin/end;
5. first frameset;
6. tenth frameset;
7. pipeline stop begin/end;
8. thread join completion;
9. startup failure and error category;
10. any recovery action.

Distinguish:

```text
warm restart:
  pipeline stop and start without a USB or firmware reset

cold/recovery restart:
  firmware reset, USB reset, reauthorization, or physical reconnect
```

A failed attempt remains part of the reliability result. A successful retry
must not replace it. Recovery time and the retried measurement are reported
separately.

Startup experiments initially use `SCHED_OTHER`, `SCHED_RR`, and
`SCHED_FIFO`. Per-thread `SCHED_DEADLINE` is applied only after stable threads
exist and is therefore primarily a steady-state treatment.

### 6.3 Main steady-state phase

```text
camera preconditioning before campaign: 5 min
warm-up per run: 10 s, excluded
measurement per run: 10 min
repetitions per condition: 5
```

At 30 frames/s:

```text
warm-up deliveries per camera: 300
measured deliveries per camera: 18000
```

At 60 frames/s:

```text
warm-up deliveries per camera: 600
measured deliveries per camera: 36000
```

The measurement begins only after every camera has completed its warm-up.
It ends after every camera has reached the requested measured delivery count.

### 6.4 Extreme long steady-state phase

```text
measurement per run: 60 min
repetitions: 1-3
```

Do not apply the one-hour phase to the complete Cartesian product. Select:

- the `SCHED_OTHER` baseline;
- the best-performing real-time policy;
- representative and stress workloads;
- the maximum feasible camera count;
- the most important CPU-noise and USB-noise conditions;
- selected best-case and worst-case configurations.

Continuous frame metrics are required for the full hour. If a full LiME trace
is too large, collect LiME windows at the beginning, middle, and end while
keeping frame timing and failure monitoring active throughout.

## 7. Noise Workloads

### 7.1 CPU busy-loop noise

Define:

- number of workers;
- scheduling policy and priority;
- CPU affinity;
- target utilization;
- start and stop timestamps.

The primary condition should use `SCHED_OTHER` workers pinned to the same
experimental CPUs as the camera threads. A pilot dose-response experiment may
use one, two, and three workers, after which one fixed saturation level should
be selected for the main matrix.

### 7.2 Shi-Tomasi corner-detection noise

Use a fixed prerecorded image sequence rather than frames from the camera
under test. Fix:

- input resolution and number of frames;
- OpenCV version and build options;
- Shi-Tomasi parameters;
- worker count;
- CPU affinity;
- whether processing is rate-limited or runs at maximum throughput;
- achieved frames/s and CPU utilization.

This represents realistic computer-vision interference, while the busy loop
provides a simpler scheduler-contention upper bound.

### 7.3 USB storage noise

Use a dedicated test file on a USB storage device. Do not write directly over
a filesystem or partition containing valuable data. Fix:

- storage device and USB port;
- whether it shares the camera xHCI controller;
- read, write, or mixed workload;
- sequential or random access;
- block size;
- queue depth;
- direct-I/O setting;
- measured bandwidth and I/O latency.

The primary USB condition should use a time-based, sustained workload that
starts before camera warm-up and continues until measurement completes. Start
the noise before a startup attempt when studying startup interference.

### 7.4 Noise timing

For steady-state experiments:

```text
noise start: at least 10 s before camera warm-up
noise stop: after camera measurement and pipeline stop
```

For startup experiments:

```text
noise start: before context and pipeline construction
noise stop: after all startup-created threads have joined
```

## 8. Measurements

### 8.1 Startup metrics

- context construction time;
- device enumeration time;
- pipeline construction time;
- `pipeline.start()` time;
- time to first frameset;
- time to tenth frameset;
- stop time;
- thread join time;
- total cycle time;
- thread count and thread lifetime;
- startup failure rate;
- recovery count and recovery duration.

### 8.2 Frame and stream metrics

For every camera and stream:

- frame count;
- frame number;
- frame-number gaps;
- inferred dropped frames;
- timeout count;
- sensor timestamp;
- host `CLOCK_BOOTTIME` timestamp;
- sensor inter-arrival distribution;
- host inter-arrival distribution;
- nominal-period error and jitter;
- maximum frame gap;
- deadline-miss count;
- active stream profile and timestamp domain.

Define a frame deadline before analysis. Report both a strict period-based
threshold and frame-number gaps so that scheduler delay is not confused with a
device-side drop.

### 8.3 Thread metrics

For every observed thread:

- creation, first execution, and exit timestamps;
- parent thread;
- thread name and TID;
- effective scheduling policy and parameters;
- CPU affinity and CPUs used;
- activation count;
- activation period or minimum inter-arrival;
- execution time per activation;
- ready-to-running latency;
- blocking and sleeping duration;
- preemption count;
- CPU migration count;
- incomplete trace coverage;
- execution-time percentiles and maximum.

Classify each thread as:

```text
startup-only
periodic steady-state
sporadic/event-driven
dormant or rarely activated
```

### 8.4 System and USB metrics

- per-core CPU utilization;
- CPU frequency and temperature;
- thermal and power throttling;
- context switches and migrations;
- xHCI and USB IRQ counts;
- UVC URB errors;
- kernel warnings;
- negotiated USB speed;
- noise CPU utilization or I/O throughput;
- memory usage;
- result and trace file sizes.

## 9. Tracing and Observer-Effect Validation

The primary tracing stack consists of:

- LiME/eBPF scheduler tracing;
- the project pthread lifecycle `LD_PRELOAD` library;
- phase markers from the benchmark probes;
- frame-event logging.

Before accepting traced results, quantify instrumentation overhead on the two
single-camera workloads:

```text
LiME + pthread LD_PRELOAD tracing
pthread LD_PRELOAD tracing only
no LiME and no LD_PRELOAD tracing
```

Use at least three repetitions per instrumentation condition. Compare:

- frame inter-arrival distributions;
- drops and timeouts;
- CPU utilization;
- pipeline start and stop time;
- total runtime.

This instrumentation factor is an observer-effect validation and does not need
to be crossed with the complete main experiment matrix.

## 10. SCHED_DEADLINE Calibration

Linux `SCHED_DEADLINE` uses runtime, relative deadline, and period. Runtime is
a CPU execution budget, not the wall-clock response time of an activation.

For a periodic thread:

```text
runtime >= conservative execution-time bound
deadline = required relative completion deadline
period <= safe activation period
```

The initial execution-time bound should be based on the largest observed
activation or a sufficiently high percentile from repeated no-noise and noise
measurements, followed by an explicit safety margin. It must not be described
as a proven WCET unless a valid WCET analysis has been performed.

Calibration procedure:

1. run the thread under `SCHED_OTHER`, `SCHED_RR`, or `SCHED_FIFO`;
2. identify stable threads and classify their activation patterns;
3. estimate period or safe minimum inter-arrival;
4. collect activation execution times across both workloads and relevant
   interference;
5. choose runtime from a conservative bound plus margin;
6. choose deadline from the frame or housekeeping requirement;
7. verify Linux admission control;
8. apply attributes to stable TIDs;
9. verify effective attributes with `sched_getattr()` or equivalent evidence;
10. record runtime depletion, overruns, throttling, and missed deadlines.

For each CPU scheduling domain, check:

```text
sum(runtime_i / period_i) < available deadline bandwidth
```

Repeat the admission calculation when the number of cameras changes.

Do not simply launch the complete application under `SCHED_DEADLINE` before it
creates librealsense threads. Deadline tasks are subject to restrictions on
creating children, and a process-wide wrapper can prevent the required threads
from being created. Start the pipelines first, identify stable threads, apply
per-thread reservations, and then begin steady-state measurement.

Sporadic and rarely activated threads require a conservative minimum
inter-arrival assumption or a suitable server reservation. Startup-only
threads should not reuse steady-state parameters.

Linux references:

- [Deadline Task Scheduling](https://www.kernel.org/doc/html/latest/scheduler/sched-deadline.html)
- [Real-Time Preemption](https://www.kernel.org/doc/html/latest/core-api/real-time/index.html)
- [How Real-Time Kernels Differ](https://www.kernel.org/doc/html/latest/core-api/real-time/differences.html)

## 11. Repetitions, Ordering, and Blocking

Kernel selection requires a reboot and should be treated as an experimental
block. Avoid collecting every non-RT result first and every RT result several
days later without accounting for the session effect.

Recommended procedure:

- use at least three boot sessions per kernel;
- verify platform controls after every reboot;
- randomize policy, workload, camera-count, and noise order within a boot
  block;
- distribute repetitions across boot sessions;
- record the random seed and realized order;
- do not silently discard interrupted or failed runs;
- rerun a failed condition only as a separately identified retry.

Start each measured run from the same logical state. Restore CPU frequency,
noise processes, USB bindings, and scheduler settings after interrupted
campaigns.

## 12. Staged Experiment Plan

A complete Cartesian product would be too large:

```text
2 kernels
x 4 policies
x 2 workloads
x 4 camera counts
x 4 noise conditions
= 256 steady-state configurations
```

At five repetitions and ten minutes per repetition, this would require more
than 213 hours of measurement before startup, warm-up, reboot, recovery, and
one-hour runs.

Use staged experiments instead.

### Stage A: parameter exploration

```text
kernel: current validated kernel
camera_count: 1
policy: OTHER, RR
workload: representative, stress
noise: none
duration: 5 min
repetitions: 3
```

### Stage B: baseline characterization

```text
kernel: default, PREEMPT_RT
camera_count: 1
policy: OTHER, RR, FIFO
workload: representative, stress
noise: none
```

Use this stage to complete the thread timing model and prepare deadline
parameters.

### Stage C: SCHED_DEADLINE calibration

```text
kernel: PREEMPT_RT first, then default
camera_count: 1
workload: representative, stress
noise: none, selected CPU noise
```

Validate reservations and admission before multi-camera scaling.

### Stage D: camera scalability

```text
kernel: default, PREEMPT_RT
camera_count: 1, 2, 3, 4
policy: OTHER and the best real-time policy
workload: representative, stress
noise: none
```

### Stage E: interference sensitivity

```text
kernel: default, PREEMPT_RT
camera_count: 1 and maximum feasible count
policy: OTHER and the best real-time policy
workload: representative, stress
noise: none, cpu_busy, shi_tomasi, usb_storage_io
```

### Stage F: long-run validation

Select the most important best-case, worst-case, baseline, and stress
conditions for 60-minute measurements.

## 13. Failure Semantics

Treat the following as experimental outcomes:

- profile resolution failure;
- pipeline start failure;
- missing first frame;
- timeout;
- frame-number gap;
- USB reset or disconnect;
- UVC URB error;
- scheduler admission failure;
- deadline runtime overrun;
- thermal or power throttling;
- failure to stop or join threads.

For multi-camera stress configurations, inability to start all requested
streams is a scalability result. Do not silently lower resolution, disable a
stream, or remove a camera. Any fallback configuration must be a new,
explicitly named condition.

## 14. Result Metadata

Every run directory should contain:

- run command and environment;
- case configuration;
- kernel and system manifest;
- camera serials, firmware, and active profiles;
- USB topology before and after;
- scheduler settings;
- CPU affinity and frequency settings;
- camera controls;
- noise configuration and achieved load;
- raw frame events;
- pthread lifecycle events;
- LiME scheduler trace or trace-window manifest;
- kernel log delta;
- parsed per-stream and per-thread summaries;
- success, failure, and recovery status.

Use stable condition identifiers containing at least:

```text
kernel
policy
workload
camera_count
noise
usb_topology
repetition
boot_session
```

## 15. Repository Tools

Startup campaigns:

```text
tools/realsense_startup_bench/run_startup_campaign.py
```

Steady-state campaigns:

```text
tools/realsense_steady_bench/run_steady_campaign.py
```

Five-minute parameter exploration:

```text
tools/realsense_steady_bench/configs/parameter_exploration_5min.json
```

The benchmark implementation should keep startup timing, steady-state frame
metrics, thread scheduler traces, topology snapshots, and recovery attempts in
separate raw artifacts so that paper figures and tables remain reproducible
from the original data.
