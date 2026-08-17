# RealSense Multi-Camera Experiment Plan

This document describes how the existing two RealSense D435 cameras, three
D455 cameras, and one D415 camera can extend the Raspberry Pi 5 experiments
for the RTNS paper.

Any campaign already in progress must finish and be backed up before these
experiments change the camera wiring, kernel, or repository state.

## 1. Research Goals

The additional cameras can answer five questions:

1. How do the librealsense thread count, USB interrupts, CPU execution time,
   memory pressure, and frame freshness scale from one to three cameras?
2. Can scheduling parameters calibrated on one physical camera be reused on
   another camera of the same model?
3. Do the D415, D435, and D455 exhibit the same worker structure, activation
   periods, and execution-time distributions?
4. Does a degradation originate in CPU scheduling, a USB root controller, a
   shared hub, or the camera model?
5. Do `SCHED_DEADLINE` and rate-monotonic RR/FIFO still improve timeliness and
   freshness in homogeneous and heterogeneous multi-camera systems?

## 2. Camera Differences

| Model | Count | Depth sensor | RGB sensor | IMU | Primary experimental use |
|---|---:|---|---|---|---|
| D435 | 2 | Wide field of view, global shutter | Rolling shutter | No | Preserve the established baseline and compare it with other models |
| D455 | 3 | Wide field of view, global shutter, 95-mm baseline | Global shutter, up to 30 FPS | Yes | Same-model scaling, profile portability, and hardware synchronization |
| D415 | 1 | Standard field of view, rolling shutter | Rolling shutter, up to 30 FPS | No | Heterogeneous-device and thread-model boundary case |

The D455 and D415 RGB sensors support at most 30 FPS. Therefore, the D435
stress configuration of `960x540 RGB8 @ 60 FPS` cannot be used unchanged for
a strict cross-model comparison. Enumerate the stream profiles on every
physical device before each formal experiment instead of assuming that a
configuration is supported from the product specification alone.

Relevant vendor documentation:

- [D400 Series Datasheet](https://dev.realsenseai.com/download/42003/)
- [D455 product specifications](https://www.realsenseai.com/products/real-sense-depth-camera-d455f/)
- [D415 product specifications](https://www.realsenseai.com/products/stereo-depth-camera-d415/)
- [D400 multi-camera configuration guide](https://dev.realsenseai.com/docs/multiple-depth-cameras-configuration/)

## 3. Common Workloads

Cross-model experiments must use a stream configuration that every tested
model actually supports.

| Workload | Stream configuration | Purpose |
|---|---|---|
| Common representative | Depth `848x480 Z16 @ 30 FPS`; Color `640x480 RGB8 @ 30 FPS`; IR disabled | Main cross-model and camera-count comparison |
| Common high-rate | Depth `848x480 Z16 @ 60 FPS`; IR1/IR2 `848x480 Y8 @ 60 FPS`; Color disabled | High-rate USB, IRQ, and scheduling stress |
| Model-specific maximum | Highest feasible combination for each model | Find device-specific limits only; do not use it to rank models directly |

`Common high-rate` is a candidate configuration. Validate profile negotiation
and sustained acquisition separately on all six cameras before formal use. If
the D415 or a firmware revision does not support this exact combination, use
the next-highest configuration shared by every model and record the selected
profiles in the run metadata.

## 4. Recommended Experiment Matrix

### 4.1 P0: Same-Model D455 Camera-Count Scaling

This is the highest-priority extension because three identical cameras
minimize model-related confounding factors.

| Variable | Values |
|---|---|
| Camera model | D455 |
| Camera count | 1, 2, 3 |
| Workload | Common representative, Common high-rate |
| Policy | `SCHED_OTHER`, tuned `SCHED_DEADLINE` |
| Repetitions | 3 |
| Duration | 10 min per run |
| Interference | None |

The matrix contains:

```text
3 camera counts x 2 workloads x 2 policies x 3 repetitions
= 36 runs
= 6 hours of measured time
```

Balance physical-device variation as follows:

- Use a different D455 for each repetition of the one-camera condition.
- Use the three distinct camera pairs for the repetitions of the two-camera
  condition.
- Use all three D455 cameras in every repetition of the three-camera condition.

If a complete policy matrix is later necessary, add RR-RM and FIFO-RM. The
four-policy matrix contains 72 runs and 12 hours of measured time.

### 4.2 P0: D455 Scheduling-Profile Portability

This experiment determines whether a timing profile is specific to a physical
device or reusable across devices of the same model.

1. Collect `SCHED_OTHER` LiME traces independently for D455-A, D455-B, and
   D455-C.
2. Compare thread signatures, instance counts, periods, maximum execution
   times, and activation distributions.
3. Generate three per-device profiles and one pooled D455 profile.
4. Apply A's profile to B and C in short smoke tests.
5. Run formal 10-minute transfer tests after the smoke tests pass.
6. Compare per-device and pooled profiles by admission result, Deadline
   overruns, duplicate frames, sequence gaps, and latency tails.

This experiment directly supports the question:

> Can a scheduling profile calibrated on one camera be safely reused across
> physical devices of the same model?

### 4.3 P1: Cross-Model Worker Comparison

| Variable | Values |
|---|---|
| Model | D415, D435, D455 |
| Camera count | 1 |
| Workload | Common representative, Common high-rate |
| Modeling policy | `SCHED_OTHER` |
| Formal policies | `SCHED_OTHER`, per-model tuned `SCHED_DEADLINE` |
| Repetitions | 3 |
| Duration | 10 min per formal run |

Compare:

- steady-state thread signatures and counts;
- each worker family's period, execution time, and blocked interval;
- USB IRQ counts and distribution;
- frame interarrival tails;
- duplicate frames, unique frames, sequence gaps, and stale framesets; and
- per-model Deadline parameters and admission results.

Only one D415 is available, so it is a heterogeneous boundary case rather than
evidence about the complete D415 population. Three D455 and two D435 devices
allow limited reporting of device-to-device variation.

### 4.4 P1: USB Topology

Prefer the three identical D455 cameras so that camera model does not become a
confounding factor.

| Topology | Configuration |
|---|---|
| Balanced | Two cameras on one USB root controller and one camera on the second controller (`2+1`) |
| Concentrated | Three cameras through one powered USB 3 hub into one controller (`3+0`) |

Test the common representative workload first, then the common high-rate
workload. Initially compare `SCHED_OTHER` and tuned `SCHED_DEADLINE`, with
three 10-minute repetitions each.

Record:

- each camera's physical USB path, speed, and root controller;
- hub model and external power source;
- xHCI IRQ affinity and IRQ-count delta;
- theoretical payload rate per root controller;
- UVC resubmit errors, USB resets, and librealsense recovery actions; and
- CPU, memory, and temperature state.

### 4.5 P1: Heterogeneous Multi-Camera Systems

Increase complexity gradually:

1. D435 + D455;
2. D435 + D455 + D415;
3. two D435 + three D455; and
4. all six cameras, only as a capacity-boundary exploration.

Formal heterogeneous comparisons should use a common workload and separate
scheduling parameters for each model, or parameters pooled by model. Do not
assume that a D435 Deadline profile is valid for D455 or D415 workers.

### 4.6 P2: D455 Hardware Synchronization

With two or three D455 cameras, compare:

- free-running/default mode;
- one master with one or two slaves; and
- if a reliable pulse source is available, an external trigger with all
  cameras in slave mode.

In addition to the existing freshness and latency metrics, record:

- cross-camera frame-timestamp skew;
- frame-counter alignment;
- the USB IRQ burst within each frame period; and
- worst-case execution and Deadline overruns under synchronization.

This experiment requires synchronization cables and reliable preservation of
hardware timestamps, sensor timestamps, and frame counters. Hardware
synchronization may improve cross-camera temporal alignment while making USB
transfers and IRQ service more concentrated.

### 4.7 P2: Maximum Camera-Count Exploration

Before formal long runs, perform a short sweep:

```text
camera count: 1, 2, 3, 4, 5, 6
duration: 2 min
policy: SCHED_OTHER
workload: common representative or a lower-bandwidth common mode
repetitions: initially 1
```

The objective is to locate the first occurrence of:

- pipeline-start failure;
- `Frame didn't arrive`;
- fallback to USB 2 speed;
- a sharp increase in duplicates or sequence gaps;
- a root-controller or hub bandwidth limit; or
- a CPU, memory, or IRQ bottleneck.

After locating the boundary, repeat formal experiments only at the last stable
camera count and the first degraded camera count.

### 4.8 P3: D455 IMU On/Off

Compare D455 image acquisition with motion streams disabled and enabled under
the same image workload. This reveals whether high-rate motion streams add
workers, wakeups, and USB/CPU load. Keep this lower-priority experiment
separate from the visual-workload matrix so that an IMU difference is not
mistaken for a camera-model effect.

## 5. Scheduling-Profile Rules

1. For a fair comparison, model D415, D435, and D455 workers separately from
   `SCHED_OTHER` traces.
2. Continue to derive Deadline runtime from an observed execution-time maximum
   plus a safety factor. Derive each period and deadline from that worker's
   shortest stable interval rather than assigning the camera FPS to every
   worker.
3. Assign RR-RM and FIFO-RM priorities by observed period. Workers with equal
   periods receive equal priorities.
4. Formal results should use a per-model profile or a validated pooled profile.
5. Applying a D435 profile to D455 or D415 is an explicit transfer or negative
   experiment, not a normal Deadline configuration.
6. A main thread that only blocks while workers run must not receive an
   artificial frame-rate Deadline period.

## 6. Fixed Experimental Controls

Reuse the established RTNS controls wherever possible:

- Raspberry Pi 5;
- Linux 6.12 BTF kernel, with standard or `PREEMPT_RT` recorded explicitly;
- V4L2 plus the `uvcvideo` backend;
- CPU frequency fixed at 1500 MHz;
- CPU0 used for housekeeping/USB IRQ service and benchmark threads on CPU1--3
  when that topology is the selected treatment;
- page cache dropped before each run;
- camera autosuspend disabled;
- identical librealsense commit, firmware records, cables, and power setup;
- full-reset recovery, with only the final successful attempt contributing to
  formal steady-state metrics;
- 10-minute formal runs; and
- topology, IRQ, thermal, frequency, freshness, and scheduler metadata saved
  for every run.

## 7. Autosuspend and Device IDs

A rule that matches only the D435 PID does not cover the new models:

```text
D435: 8086:0b07
D455: 8086:0b5c
D415: 8086:0ad3
```

After an active campaign finishes, add the D455 and D415 PIDs to the dedicated
no-autosuspend udev rules. Do not use an overly broad rule that matches the
Intel vendor ID `8086` alone, because it could affect unrelated Intel USB
devices.

## 8. Required Probe and Benchmark Extensions

Before formal experiments on the new models:

1. Generalize model-specific stream-mode names such as `d435_all`.
2. Enumerate and save the stream profiles supported by each physical camera.
3. Support a common workload on different cameras and, if needed, per-camera
   stream configurations.
4. Extend autosuspend handling and full-reset recovery to the D415 and D455
   PIDs.
5. Require every camera to complete startup and warmup before measurement.
6. Start interference only after all cameras finish warmup.
7. Record each camera's serial, model, firmware, USB path, and freshness
   metrics separately.
8. Support per-model scheduling profiles or model-grouped profile entries.
9. Add timestamp-skew and frame-counter-alignment metrics for hardware
   synchronization.
10. Preserve failed attempts without mixing them into a successful run's
    steady-state statistics.

## 9. Recommended Execution Order

1. Finish the current campaign and verify its laptop backup.
2. Enumerate all six cameras' serials, PIDs, firmware, USB speed, and stream
   profiles.
3. Add D415/D455 autosuspend rules and model-independent workload support.
4. Run a short single-camera smoke test on every device.
5. Complete the D455 `1/2/3` camera-count P0 matrix.
6. Complete the three-D455 profile-portability experiment.
7. Complete the balanced/concentrated topology experiment with three D455
   cameras.
8. Complete the single-camera cross-model comparison.
9. Use those results to decide whether formal heterogeneous 3--6 camera,
   hardware-synchronization, and IMU experiments are justified.

This order first extracts the cleanest and easiest-to-interpret result from
three identical D455 cameras, then introduces camera-model and USB-topology
confounders one at a time.
