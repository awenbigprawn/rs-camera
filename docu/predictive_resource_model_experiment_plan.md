# Predictive Resource Model and Validation Plan

## 1. Objective

The model should predict whether a selected RealSense camera topology and
stream configuration can deliver complete, fresh framesets on a Raspberry Pi
5. It should answer three practical questions before running the full
application:

1. How many userspace and kernel execution contexts will the configuration
   activate?
2. How much CPU, USB-controller, and host-memory demand will it create?
3. At what added CPU, memory, or GPU pressure should freshness or timing begin
   to degrade?

The predictions are empirical engineering estimates. They are fitted from
measured maxima, minima, and medians and are not WCET bounds or hard
schedulability guarantees.

## 2. Test Platform and Current Topology

The calibration and validation use the Raspberry Pi 5 RT kernel
`6.12.96-rpi5-rt-btf-uvc16+`, the V4L2 backend, `uvcvideo` with 16 URBs, a
fixed 1500 MHz CPU frequency, and no CPU affinity, taskset, or CPU isolation.
Each logical run uses a 30-frame warm-up, a fixed 30-second measurement
window, full-reset recovery, and up to three startup attempts. Noise starts
only after all cameras pass the warm-up freshness check.

The four-camera validation topology is balanced across the two SuperSpeed
root controllers:

| Root controller | Hub port | Model | librealsense serial | USB descriptor serial |
|---|---|---|---|---|
| `xhci-hcd.0`, Bus 3 | `3-1.2` | D455F | `311322304911` | `327743060882` |
| `xhci-hcd.0`, Bus 3 | `3-1.3` | D455F | `311322304863` | `327743063467` |
| `xhci-hcd.1`, Bus 5 | `5-1.2` | D455F | `311322302503` | `327743062558` |
| `xhci-hcd.1`, Bus 5 | `5-1.3` | D435 | `948122073863` | `948123020218` |

All four links enumerate at 5 Gb/s and USB autosuspend is disabled before
measurement.

## 3. Topology-Aware Worker Model

A camera count alone cannot predict the number of execution contexts because
some work is shared and some work is replicated. For worker family `i`, use

```text
n_i(X) = a_i
       + sum_m b_i,m N_m
       + sum_s c_i,s N_s
       + sum_h d_i,h I_h
```

where:

- `a_i` is process-wide work, such as the process main thread or a shared
  device watcher;
- `N_m` is the number of cameras of model `m` and `b_i,m` is a per-camera
  contribution;
- `N_s` is the number of enabled receiver paths of stream type `s` and
  `c_i,s` is a per-stream contribution;
- `I_h` indicates that root controller `h` is active and `d_i,h` represents
  per-controller kernel service, including xHCI IRQ service;
- shared unbound kernel workqueues are reported as pools, rather than being
  incorrectly multiplied once per camera.

The 1-camera traces identify process-wide plus per-camera/per-stream terms.
The 2-camera traces on one controller reveal which terms replicate and which
remain shared. Creator-stack signatures, stream ablation, and the receive-path
trace distinguish the terms. The 4-camera trace is withheld as validation.

## 4. CPU Model

For userspace worker family `i`, the measured CPU demand is

```text
U_sdk(X) = sum_i n_i(X) C_i / T_i + Delta_interaction(X)
```

`C_i` is an observed execution-time statistic and `T_i` is the observed stable
activation interval. `Delta_interaction` is fitted from the difference between
the 2-camera observation and the sum of two independent 1-camera estimates. It
captures concurrency effects such as synchronization, conversion, copying,
and cache interference without pretending that all cameras scale linearly.

Kernel USB demand is modeled per root controller:

```text
U_kernel,h(X) = U_irq,h + U_softirq,h + U_uvc_work,h
U_cpu(X)      = U_sdk(X) + sum_h U_kernel,h(X)
```

The average utilization is supplemented with peak-window execution and ready
delay. xHCI service is sporadic and bursty, so average utilization alone does
not establish whether its short service bursts can meet the receive-path
precedence constraint.

## 5. USB-Controller Model

For controller `h`, the configured payload is

```text
B_usb,h(X) = sum_(camera c on h) sum_(stream s on c)
             fps_s width_s height_s bytes_per_wire_pixel_s
```

The model is evaluated per controller, not only as a system-wide sum. A
balanced four-camera topology can therefore differ from placing all cameras
behind one hub. Payload is converted to an empirical controller demand using
the measured protocol/transfer overhead from the 1- and 2-camera traces. The
sustainable limit is an experimentally calibrated operating boundary, not the
nominal 5-Gb/s line rate.

## 6. Host-Memory Model

The static byte-touch expression is a lower bound:

```text
B_touch(X) = USB DMA writes
           + depth/IR reads
           + color-input reads
           + RGB output writes
           + identified SDK copies
```

Hardware counters and the rate-limited fixed-copy workload are used to fit an
empirical inflation factor from `B_touch` to observed shared-memory demand.
For a measured sustainable memory-service budget `B_safe`, the predicted
headroom is

```text
H_mem(X) = B_safe - B_camera(X).
```

The predicted memory-noise transition should occur near `H_mem`. The
fixed-copy worker reports its achieved read-plus-write traffic, which is used
instead of the requested rate. Existing two-D435 results place detectable
degradation near 0.95 GiB/s of added traffic and a severe knee near
2.0--2.66 GiB/s. The four-camera sweep therefore starts at lower levels.

## 7. Calibration Matrix

Both workloads are measured under `SCHED_OTHER`, without interference, with
three repetitions and LiME enabled.

| Calibration group | Camera topology | Representative 30 FPS | Common stress 60 FPS | Purpose |
|---|---|---:|---:|---|
| D435 x1 | Bus 5, one camera active | 3 runs | 3 runs | D435 per-camera and per-stream terms |
| D455 x1 | Bus 5, one camera active | 3 runs | 3 runs | D455 per-camera and per-stream terms |
| D455 x2 | Bus 3, same hub/controller | 3 runs | overload point | shared terms at 30 FPS and a controller-local capacity boundary at 60 FPS |

The representative workload enables depth 848x480 Z16 at 30 FPS and color
640x480 RGB8 at 30 FPS. The common stress workload enables depth, IR1, and IR2
at 848x480 and 60 FPS, with color disabled. This profile is supported by both
selected camera models and avoids changing the stream profile together with
the camera count. The separate all-stream 60-FPS point adds 848x480 RGB8 and
serves as an observed overload configuration.

## 8. Four-Camera Validation

The model fitted only from the successful 1- and 2-camera runs predicts the 3-D455 plus
1-D435 configuration. The validation then compares predicted and measured:

- live userspace worker families and instances;
- active root-controller/kernel execution contexts;
- SDK and kernel CPU utilization;
- per-controller USB payload and event rates;
- analytical and observed memory demand;
- inter-delivery p99 and maximum;
- duplicate frames, sequence gaps, partially stale framesets, and timeouts.

One LiME run of the feasible representative workload validates the held-out
four-camera topology. Three low-overhead repetitions validate its timing and
freshness without retaining a large scheduler trace for every repetition.
The 60-FPS configurations that cannot pass the warm-up freshness gate are
retained as censored overload observations rather than treated as successful
steady-state traces.

The initial preflight adds two censored capacity observations. Two D455
cameras behind one controller could not pass the Depth+IR 60-FPS freshness
gate in three fresh-start attempts. The balanced four-camera topology also
failed both Depth+IR and all-stream 60-FPS profiles in three attempts, while
its representative 30-FPS profile completed without a freshness error. The
model must place the successful and failed points on the correct sides of its
predicted feasibility boundary.

The following held-out ladder narrows that boundary. Payload assumes Z16 and
YUYV at two wire bytes per pixel and Y8 at one byte per pixel. Each root
controller serves two cameras in the four-camera topology.

| Four-camera profile | Payload per camera | Payload per controller | Preflight state | Identification purpose |
|---|---:|---:|---|---|
| Depth+color, 30 FPS | 40.87 MiB/s | 81.74 MiB/s | successful | feasible reference |
| Depth only, 60 FPS | 46.58 MiB/s | 93.16 MiB/s | pending | high activation rate, one receiver path |
| Depth+IR, 30 FPS | 46.58 MiB/s | 93.16 MiB/s | pending | same payload, three receiver paths |
| Depth+color, 60 FPS | 81.74 MiB/s | 163.48 MiB/s | pending | intermediate controller demand |
| Depth+IR, 60 FPS | 93.16 MiB/s | 186.33 MiB/s | failed freshness gate | controller-capacity overload point |
| Depth+IR+color, 60 FPS | 139.75 MiB/s | 279.49 MiB/s | failed freshness gate | explicit overload point |

The equal-payload depth-only and Depth+IR profiles distinguish a controller
payload limit from additional per-stream workers, buffer queues, and callback
work. The 60-FPS depth+color point lies between the known feasible and failed
controller demands and therefore sharpens the empirical USB operating bound.

## 9. Independent Pressure Sweeps

Each sweep changes one interference source while the four-camera workload and
all other controls remain fixed. Screening levels use one repetition. The
last normal level and first degraded level are repeated three times.

| Resource | Screening levels | Reported intensity |
|---|---|---|
| CPU | 0, 1, 2, 3, 4 register-only workers | measured CPU equivalents and ready delay |
| Memory | 0, 250, 500, 750, 1000 MiB/s; then midpoint refinement | achieved read-plus-write MiB/s |
| GPU/shared memory | deferred; if time remains, compare no GPU load with the existing full-rate MobileNetV2 Vulkan load | achieved inference rate |

The experiment does not use USB-storage noise. GPU intensity control is not on
the critical path for this campaign. If the optional full-load comparison is
run, it is interpreted as a GPU plus shared-memory competitor rather than as a
pure GPU resource test.

## 10. Outcome Classification

The baseline distribution defines the timing reference separately for each
workload. A run is classified as:

- **normal** when startup and measurement succeed, all framesets are fresh,
  and p99 remains within the baseline confidence band;
- **timing-degraded** when freshness remains intact but p99 or the maximum
  exceeds the preregistered baseline-relative threshold;
- **freshness-degraded** when any duplicate, sequence gap, partially stale
  frameset, or measurement timeout occurs;
- **failed** when all retry attempts fail to enter or complete measurement.

The observed first degraded pressure is compared with the model prediction.
The comparison should report error intervals and run-to-run variability rather
than claim an exact deterministic boundary.

## 11. Scheduling-Policy Invariance Check

The worker model is first fitted under `SCHED_OTHER`. A separate experiment
tests whether the same structural and activation model remains valid under
role-aware `SCHED_RR`, `SCHED_FIFO`, and `SCHED_DEADLINE`. The feasible
calibration cases are run for 30 seconds with three repetitions under four
treatments: OTHER, RR with rate-monotonic priorities, FIFO with
rate-monotonic priorities, and Deadline with the profile generated from the
three contemporaneous OTHER traces. The xHCI IRQ threads are set to FIFO
priority 90 before those OTHER traces are collected. They retain that policy
and their original CPU affinities for all four treatments, so the SDK worker
policy is the only scheduling factor that changes.

"The same time model" has a narrow, testable meaning:

- normalized creator signatures have the same worker-family multiplicities;
- families classified as stable retain their principal activation periods;
- the creation and receive-path precedence relations remain unchanged.

The comparison does not require equal execution time, ready delay, or response
time. Those distributions may change because the scheduler changes
preemption, cache state, and interference. Activations separated only by a
short intra-burst interval are first merged into logical jobs with the same
reconstruction used by the scheduler-profile generator. Each reconstructed
logical period is compared with the median OTHER estimate. A 5% relative
difference is used as a declared descriptive tolerance, not as a hard timing
or schedulability guarantee.

The main probe thread stays under OTHER because it blocks while controlling
the run and is not a periodic capture worker. Under the Deadline treatment,
the modeled SDK workers use Deadline reservations, while the sporadic and
bursty xHCI IRQ predecessors use FIFO priority 90. No CPU affinity, taskset,
cgroup partition, or CPU isolation is applied.

The policy matrix covers single D435 and D455 cameras under representative and
common-stress workloads, plus the feasible two-D455 representative workload.
The overloaded two-D455 60-FPS point is excluded because it cannot enter a
valid steady state from which to estimate an activation model.

## 12. Immediate Execution Order

1. Verify the common four-camera 60-FPS profile with a short preflight.
2. Collect the 1- and 2-camera calibration traces and back them up to the
   laptop before removing them from the Pi.
3. Fit the worker, CPU, USB, and memory coefficients.
4. Predict the four-camera baseline and memory headroom before reading the
   four-camera validation result.
5. Run the four-camera baseline and independent pressure sweeps.
6. Validate the time model under RR-RM, FIFO-RM, and Deadline.
7. Refine and repeat only the observed transition intervals.
8. Back up each completed batch and checksum it before freeing Pi space.
