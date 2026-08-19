# Three-D455 Split-Controller Resource-Model Validation (2026-08-18)

## 1. Validation Question

This experiment tests whether the resource model fitted from one- and
two-D455 traces predicts a held-out three-D455 workload.  The three cameras
are distributed across both Raspberry Pi 5 xHCI controllers.  Two cameras use
`xhci-hcd.0`, and one camera uses `xhci-hcd.1`.

The prediction was fixed before the held-out results were inspected:

```text
N3 = 2*N2 - N1
```

This affine expression is applied to the child-thread count and measured
userspace CPU utilization.  USB payload and memory touches are calculated
from the enabled stream profiles.

## 2. Topology and Workload

The test used kernel `6.12.96-rpi5-rt-btf-uvc16+`, the V4L2 backend, UVC16,
`SCHED_OTHER`, a fixed 1.5-GHz CPU frequency, no acquisition affinity or CPU
isolation, 30 warm-up frames, and a fixed 30-s measurement interval.  USB
autosuspend was disabled.  Each logical run began with a full reset, and all
three formal repetitions used LiME and the pthread lifecycle interposer.

| xHCI controller | USB port | SDK serial | Streams |
|---|---|---|---|
| `xhci-hcd.0` | `3-1.2` | `311322304911` | Depth 848x480 Z16 + Color 640x480 RGB8 at 30 FPS |
| `xhci-hcd.0` | `3-1.3` | `311322304863` | Depth 848x480 Z16 + Color 640x480 RGB8 at 30 FPS |
| `xhci-hcd.1` | `5-1.2` | `311322302503` | Depth 848x480 Z16 + Color 640x480 RGB8 at 30 FPS |

An initial attempt without a reset failed during UVC probe negotiation with
`-32` (`EPIPE`).  One clean full-reset smoke test then passed, so the formal
campaign retained the same full-reset gate used by the other steady-state
experiments.

## 3. Userspace Prediction and Observation

The calibration medians were 12 child threads and 0.045254 core equivalents
for one D455, and 23 child threads and 0.096369 core equivalents for two
D455 cameras.  They predict 34 child threads and 0.147485 core equivalents
for three cameras.

| Quantity | Prediction | Held-out observation | Prediction error |
|---|---:|---:|---:|
| Child threads | 34 | 34 | 0.00% |
| Userspace CPU utilization | 0.147485 cores | 0.153955 cores | +4.39% |
| Delivery interarrival p99 | -- | 33.672 ms | -- |
| Delivery interarrival maximum | -- | 35.430 ms | -- |
| Successful runs | 3 | 3 | -- |
| Duplicate/gap/stale/timeout events | -- | 0/0/0/0 | -- |

The per-run userspace utilizations were 0.152281, 0.161765, and 0.153955 core
equivalents.  All runs entered steady state on their first measured attempt.
Every returned frameset was fully fresh, and no sequence gap or timeout was
observed.  The model therefore predicts the recurring thread count exactly
and predicts total userspace CPU demand within 4.39% at this held-out point.

The calibration and validation binaries came from the same Git commit but
from different dirty source states.  Their creator-stack signatures contain
different shared-library offsets.  The aggregate thread and CPU measurements
remain comparable, but a strict family-by-family residual table would be
misleading.  Such a comparison requires rerunning all cells from one binary.

## 4. USB and Memory Demand

For one camera, the configured streams carry 40.869 MiB/s of image payload.
The model assigns 166.553 MiB/s of analytical memory touches to the identified
receive, conversion, and delivery path.  The three-camera nominal demand is:

| Controller group | Cameras | USB payload | Attributed analytical memory touch |
|---|---:|---:|---:|
| `xhci-hcd.0` | 2 | 81.738 MiB/s | 333.105 MiB/s |
| `xhci-hcd.1` | 1 | 40.869 MiB/s | 166.553 MiB/s |
| Total | 3 | 122.607 MiB/s | 499.658 MiB/s |

Each formal run returned 2,698 fully fresh per-camera framesets during about
30.000 s.  Instantiating the same byte-count equations with this delivered
frame count gives 122.516 MiB/s of image payload and 499.287 MiB/s of memory
touches, 0.074% below the nominal 30-FPS prediction.  This difference is the
finite-window frame-boundary effect, not a bandwidth loss.

These are model-derived byte rates.  USB payload excludes protocol and packet
overheads, while memory touch is not a DRAM hardware-counter measurement.
Consequently, this experiment validates the configured and delivered data
volume but does not independently measure USB line utilization or DRAM
traffic.

## 5. Per-Controller IRQ Demand

The topology-matched calibration used the two-D455 controller median for
`xhci-hcd.0` and the one-D455 controller median for `xhci-hcd.1`.  The held-out
experiment produced the following IRQ-counter deltas:

| xHCI controller | Calibration prediction per 30 s | Held-out runs | Held-out median | Difference |
|---|---:|---:|---:|---:|
| `xhci-hcd.0`, two cameras | 86,603 | 89,767 / 92,414 / 92,060 | 92,060 | +6.30% |
| `xhci-hcd.1`, one camera | 42,272 | 47,544 / 50,031 / 47,548 | 47,548 | +12.48% |
| Both controllers | 128,875 | 137,311 / 142,445 / 139,608 | 139,608 | +8.33% |

The held-out IRQ count is higher than the calibration estimate on both
controllers.  IRQ delivery is bursty, and the campaign delta is broader than
the exact steady-state gate.  Startup, stop, controller background traffic,
and interrupt coalescing can therefore affect this counter.  It should be
modeled with a measured range or safety margin rather than as an exact linear
rate.  The formal LiME run did not record kernel IRQ-thread execution time, so
this experiment does not validate the earlier coarse xHCI CPU estimate.

## 6. Conclusion and Limits

The split topology is feasible after a clean reset.  At the tested
representative workload, the empirical model accurately predicts userspace
thread multiplicity and predicts aggregate userspace CPU utilization within
4.39%.  The stream equations also reproduce the delivered image volume.
Per-controller IRQ counts are less predictable and require explicit empirical
headroom.

This is a short 30-s validation, not a WCET bound, a schedulability proof, or
a long-run reliability result.  It also does not validate physical USB line
occupancy, DRAM bandwidth, or xHCI CPU utilization with hardware counters.

## 7. Artifacts

The laptop backup is stored in:

```text
results/rpi5/d455_n3_split_resource_validation_20260818/
```

The generated aggregate analysis is:

```text
results/rpi5/d455_n3_split_resource_validation_20260818/model_validation_analysis.md
```

The reset smoke-test backup is:

```text
results/rpi5/d455_n3_split_reset_smoke_20260818/
```

The reusable case, runner, and analyzer are:

```text
tools/realsense_steady_bench/configs/predictive_resource_model_30s.json
tools/realsense_steady_bench/run_d455_n3_resource_validation.sh
tools/realsense_steady_bench/analyze_predictive_resource_model.py
```

## 8. FIFO Rate-Monotonic Follow-Up

A second held-out test examined whether FIFO rate-monotonic scheduling makes
the affine CPU prediction more accurate.  Before these three-camera FIFO runs,
the medians of the existing one- and two-D455 FIFO-RM calibration cells fixed
the prediction as:

```text
U_FIFO(N3) = 2*U_FIFO(N2) - U_FIFO(N1)
           = 2*0.096785916 - 0.045362123
           = 0.148209709 core equivalents.
```

The previously collected three-D455 `SCHED_OTHER` traces supplied only the
worker periods needed to generate the RM priority profile.  No three-D455 FIFO
result was used to set the CPU prediction.  All 34 recurring workers received
FIFO priorities from 74 through 80, while both xHCI IRQ threads used FIFO
priority 90.  The main control thread remained `SCHED_OTHER`.

| Quantity | FIFO-RM prediction | FIFO-RM observation | Prediction error |
|---|---:|---:|---:|
| Child threads | 34 | 34 | 0.00% |
| Userspace CPU utilization | 0.148210 cores | 0.156849 cores | +5.83% |
| Delivery interarrival p99 | -- | 33.443 ms | -- |
| Delivery interarrival maximum | -- | 33.548 ms | -- |
| Duplicate/gap/stale/timeout events | 0/0/0/0 | 0/0/0/0 | -- |

The three utilization observations were 0.156849, 0.153994, and 0.162137 core
equivalents.  All three runs succeeded on their first measured attempt, and
all 34 workers received their configured priorities.  The aggregate CPU
prediction error is 5.83%, compared with 4.39% under `SCHED_OTHER`.  FIFO-RM
therefore did not make the affine CPU-demand extrapolation more accurate in
this test.

FIFO-RM did make delivery timing tighter.  Relative to the corresponding
three-camera `SCHED_OTHER` experiment, the median p99 decreased from 33.672 to
33.443 ms, and the median per-run maximum decreased from 35.430 to 33.548 ms.
The IRQ-count median was almost unchanged: 139,546 across both controllers
under FIFO-RM versus 139,608 under `SCHED_OTHER`.

This distinction is important.  RM priorities reduce ready delay and bound
scheduler interference among runnable acquisition workers, but they do not
make the CPU execution cost of frame conversion, memory access, or cache
misses strictly additive across cameras.  Scheduling improves temporal
delivery without removing the multicamera execution-cost residual from the
resource model.

The FIFO-RM artifacts and runner are stored in:

```text
results/rpi5/d455_n3_split_fifo_rm_validation_20260818/
tools/realsense_steady_bench/run_d455_n3_fifo_rm_validation.sh
```

## 9. Data-Age Prediction

For this analysis, a frameset's data age is the time from the oldest component
frame's backend timestamp to the return of `wait_for_frames()`.  It is computed
for every camera and delivery as:

```text
A = max_stream(host_return_time - backend_timestamp_stream).
```

This is an application-visible age proxy.  It does not include an unknown
offset between physical exposure start and the backend timestamp.

An exploratory affine predictor uses the medians of the one- and two-D455
FIFO-RM calibration cells:

| Data-age statistic | One D455 | Two D455 | Three-D455 prediction |
|---|---:|---:|---:|
| Mean | 29.714 ms | 29.796 ms | 29.877 ms |
| p50 | 29.711 ms | 29.788 ms | 29.866 ms |
| p99 | 30.545 ms | 30.642 ms | 30.739 ms |
| Per-run maximum | 30.649 ms | 30.720 ms | 30.791 ms |

The three FIFO-RM validation runs produced:

| Run | Mean age | p50 | p99 | Maximum | p99 error from affine prediction |
|---|---:|---:|---:|---:|---:|
| 1 | 29.843 ms | 29.832 ms | 30.689 ms | 30.835 ms | -0.16% |
| 2 | 34.300 ms | 30.077 ms | 46.066 ms | 46.848 ms | +49.86% |
| 3 | 29.936 ms | 29.933 ms | 30.827 ms | 30.975 ms | +0.29% |

The median-across-runs p99 is 30.827 ms, only 0.29% above the affine
prediction.  This central result is misleading if reported alone.  Run 2
contains a valid but unfavorable depth/color phase on camera
`311322304911`.  Its depth age has a mean of 43.231 ms and a p99 of 46.311 ms,
while its color age remains near 29.7 ms.  The camera has no duplicate,
sequence-gap, stale, or timeout event, and its delivery interval remains near
33.3 ms.  Thus, this is not an overloaded CPU or a missing frame.

The trace shows that Color's backend timestamp is normally 10--18 ms later
than Depth's in this run, with a median offset of 14 ms.  The other cameras
and runs have approximately 0-ms backend skew.  The source code explains this
mode.  `timestamp_composite_matcher::are_equivalent()` in
`deps/librealsense/src/sync.cpp` accepts timestamps whose difference is less
than half of one frame period.  At 30 FPS, the permitted phase term is
approximately 16.667 ms.

Adding this synchronizer term gives an empirical phase-aware envelope:

```text
A_p99_envelope = 30.739 + 16.667 = 47.406 ms
A_max_envelope = 30.791 + 16.667 = 47.457 ms.
```

The observed worst-run p99 of 46.066 ms is 1.340 ms below the p99 envelope,
and the observed maximum of 46.848 ms is 0.609 ms below the maximum envelope.
This envelope is consistent with the held-out trace, but it is an empirical
engineering bound rather than a formal worst-case guarantee.

The conclusion is therefore twofold.  The affine model predicts aligned-phase
data age accurately, but it does not predict the tail across independent
camera restarts.  A useful data-age model must add an explicit stream-phase
and frameset-join term.  FIFO-RM controls runnable CPU service but cannot
remove a depth/color phase offset created before the userspace scheduler sees
the completed frames.
