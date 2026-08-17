# RTNS Paper: Completed Experiments and Supported Conclusions

Last updated: 2026-08-08

## 1. Purpose

This document inventories the completed RealSense D435 experiments, their
data locations, the conclusions that they support for the RTNS paper, and the
claims that remain too strong.

Evidence is classified as:

1. **Formal evidence:** controlled conditions, at least three independent
   repetitions, normally 600 s each, current freshness schema, and complete
   machine-readable artifacts.
2. **Diagnostic evidence:** a targeted failure-path or boundary experiment
   that explains mechanism but may lack formal repetition or randomization.
3. **Smoke or superseded evidence:** code, recovery, admission, or exploratory
   tests that must not enter final statistical figures directly.

Preserve this distinction in the paper. In particular, CSV `success=True`
means that a benchmark process completed; it does not prove that every
frameset was fresh, continuous, and loss-free.

## 2. Common Experimental Semantics

### 2.1 Main Platform

The most complete formal steady-state data at this update used:

| Item | Configuration |
|---|---|
| Host | Raspberry Pi 5, Ubuntu Server 24.04 |
| Cameras | Two Intel RealSense D435, firmware 5.17.3.10 |
| Librealsense | 2.58.3 |
| Backend | V4L2 plus Linux `uvcvideo` |
| USB | Cameras directly attached to independent SuperSpeed xHCI controllers |
| CPU frequency | Fixed at 1500 MHz |
| CPU/IRQ topology | xHCI IRQ threads on CPU0; benchmark and librealsense on CPU1--3 |
| USB power | Autosuspend disabled for both cameras |
| Formal RT kernel | `6.12.96-rpi5-rt-btf-uvc16+`: PREEMPT_RT, HZ=1000, BTF, `UVC_URBS=16` |
| Historical non-RT control | `6.12.96-rpi5-standard-btf+`: PREEMPT, HZ=1000, BTF, `UVC_URBS=5` |

Camera serials and paths:

- `948122073863` on USB path `3-1`;
- `327122075717` on USB path `5-1`.

### 2.2 Workloads

| Workload | Streams per camera |
|---|---|
| Representative 30 FPS | Depth 848x480 Z16 at 30 FPS; Color 640x480 RGB8 at 30 FPS; IR disabled |
| Stress 60 FPS | Depth 848x480 Z16, IR1/IR2 848x480 Y8, and Color 960x540 RGB8, all at 60 FPS |

`observed_frames` and `unique_frames` count per-stream frame objects, not
framesets. A two-camera stress delivery can contain two cameras times four
streams, or eight frame objects.

### 2.3 Freshness Metrics

Formal analysis uses:

| Metric | Meaning |
|---|---|
| `observed_frames` | All frame objects observed during measurement |
| `unique_frames` | Frames deduplicated by camera, stream, and frame number |
| `duplicate_frames` | Repeated observations of an existing frame number |
| `sequence_gaps` | Missing numbers within each stream's observed minimum-to-maximum range |
| `fully_fresh_framesets` | Every required stream advanced |
| `partially_stale_framesets` | At least one stream advanced and one did not |
| `stale_framesets` | No stream advanced |
| `measurement_timeouts` | `wait_for_frames()` timed out in measurement |

Deduplication and sorting occur after `steady_state_end`, outside the measured
loop, so set lookup and sorting do not perturb steady acquisition.

## 3. Completed Experiment Inventory

| ID | Experiment | Scale | Evidence class | Purpose |
|---|---|---:|---|---|
| E1 | Startup timeout/wait calibration | Multiple candidates, ten cycles each, three final validations | Diagnostic | Select short but sufficient waits |
| E2 | x86 D435 startup worker model | 20 independent runs | Formal structural evidence | 22 roles and creation/run/sleep/exit timeline |
| E3 | Pi 5 startup policy campaigns | 20 runs for OTHER/RR/FIFO on standard and RT | Secondary formal | Startup reliability and policy sensitivity |
| E4 | Startup recovery | USB reset, depth-prime, and full-reset smoke tests | Diagnostic | Retry only a failed logical run |
| E5 | IRQ topology/profile calibration | Three 60-s calibrations per workload plus smoke tests | Diagnostic | Establish CPU0 IRQ and CPU1--3 application topology |
| E6 | Historical RT/UVC5 matrix | 42 ten-minute and two 60-minute runs | Historical formal; partly superseded | OTHER, RR-RM, FIFO-RM, old Deadline, and interference |
| E7 | Standard/UVC5 matched matrix | 24 ten-minute runs plus calibration | Historical formal | Standard-versus-RT control |
| E8 | Memory-bandwidth boundary | 0--8000 MiB/s targets, 30/60-s levels | Diagnostic | Locate contention onset and severe knee |
| E9 | Freshness-path tracing | Multiple targeted 60/600-s traces | Strong diagnostic | Link duplicate/gap to incomplete depth UVC payload |
| E10 | UVC pool 5-versus-16 pilot | UVC5: 2 x 600 s; UVC16: 3 x 600 s | Preliminary A/B | Evaluate UVC16 as mitigation |
| E11 | RT/UVC16 OTHER calibration | 3 x 600 s per workload | Formal | Generate final per-family Deadline profiles |
| E12 | RT/UVC16 Deadline gate and validation | Gate plus 3 x 600 s per workload | Strongest formal evidence at this date | Validate budgets, timing, and freshness |

## 4. Start-Phase Evidence

### 4.1 Timeout Calibration

The calibrator varied recovery settle, first-frame timeout, join timeout, and
cycle delay. One small calibration provisionally selected:

- `frame_timeout_ms = 1000`;
- `join_timeout_ms = 10`;
- `cycle_delay_ms = 0`;
- `recovery_settle_ms = 0`;
- `enumeration_timeout_ms = 1200`; and
- `reset_timeout_ms = 5000`.

A later 20-run model observed a 1074.370-ms first-frame wait, showing that
1000 ms was too short for the tail. Production defaults became 1500 ms,
10 ms, 0 ms, and 0 ms respectively. Small calibration produces empirical
parameters, not worst-case bounds; roughly 50% headroom was necessary here.

Data:

```text
tools/realsense_startup_bench/results/timing_calibration_run1/
tools/realsense_startup_bench/results/calibrated_defaults_smoke1/
```

### 4.2 Twenty-Run Startup Worker Model

This experiment used an x86 laptop, Linux 6.8 PREEMPT_DYNAMIC,
`SCHED_OTHER`, and one D435. Each independent process enabled four 30-FPS
streams, received 60 framesets, stopped, and waited for all child threads.
LiME recorded scheduler state; the `LD_PRELOAD` interposer recorded pthread
create/start/name/join/exit and creator stacks.

All 20 runs succeeded on their first attempt and had the same modal shape:

- one main plus 21 child-thread instances, or 22 roles;
- five recurring periodic roles;
- three short-lived `libusb_event` transient roles;
- thirteen long-lived event-driven or non-periodic roles;
- every recurring role observed in all 20 runs; and
- every child terminated normally during shutdown.

Times from application `process_start`:

| Event | Median [p05, p95] | Observed maximum |
|---|---:|---:|
| `pipeline::start()` duration | 39.389 [24.802, 42.397] ms | 42.974 ms |
| First-frameset wait | 516.843 [479.263, 576.325] ms | 1074.370 ms |
| First frameset from process start | 609.070 [544.506, 670.702] ms | 1167.508 ms |
| All recurring roles stable | 664.423 [532.663, 730.765] ms | 1268.436 ms |
| Join wait | 0.393 [0.222, 0.526] ms | 0.718 ms |

Supported conclusions:

1. Return from `pipeline::start()` is not startup completion. First complete
   delivery and recurring-worker convergence occur later.
2. The first-frame wait dominates startup latency, not the start call itself.
3. The pipeline combines startup-only, event-driven, timer-driven, and frame-
   driven work rather than one periodic task.
4. Worker set and creation order were highly repeatable for this workload.

Absolute times apply only to this x86 host, camera, workload, and policy.

```text
tools/realsense_startup_bench/results/startup_model_other_20runs_20260723/model/startup_thread_model.md
tools/realsense_startup_bench/results/startup_model_other_20runs_20260723/model/startup_thread_model.csv
tools/realsense_startup_bench/results/startup_model_other_20runs_20260723/model/startup_timeline.svg
```

### 4.3 Raspberry Pi Startup Policy Campaigns

Under fixed autosuspend and V4L2 conditions, standard and PREEMPT_RT kernels
each completed 20 runs of OTHER, RR, and FIFO with no final failures.

| Kernel/campaign | Policy | `start_call_ms_mean` | `first_frame_wait_ms_mean` | `first_frame_ms_mean` |
|---|---|---:|---:|---:|
| Standard BTF | OTHER | 65.309 ms | 525.904 ms | 640.704 ms |
| Standard BTF | RR | 66.040 ms | 507.875 ms | 624.212 ms |
| Standard BTF | FIFO | 66.271 ms | 505.254 ms | 621.835 ms |
| RT BTF, corresponding early campaign | OTHER | 154.416 ms | 481.104 ms | 687.850 ms |
| RT BTF, corresponding early campaign | RR | 158.156 ms | 478.846 ms | 689.317 ms |
| RT BTF, corresponding early campaign | FIFO | 157.152 ms | 469.001 ms | 678.451 ms |

Startup is host- and kernel-sensitive, but the table does not prove that RT
policies make startup faster. The RT start call is longer, the first-frame
wait slightly shorter, and the overall advantage inconsistent. These are also
older software/kernel configurations. Use them to show variability rather
than final kernel causality.

### 4.4 Recovery

After an early `Frame didn't arrive within 15000`, later runs often failed in
sequence and depth was the first stream to stop in Viewer. Tests included
whole-device `usbreset`, sysfs authorization toggling, direct depth V4L2
acquisition, firmware reset, and firmware plus composite USB reset.

Neither USB reset nor authorization alone recovered reliably. Depth-prime was
occasionally effective. Full-reset now performs firmware reset, composite USB
reset, and re-enumeration, then retries only the current logical run. Smoke
tests observed attempt one time out and attempt two succeed completely.

This mechanism makes campaigns robust, but failed attempts remain separate
from successful data. Recovery latency must not enter the normal startup
distribution.

## 5. Steady Workers and Scheduling Parameters

### 5.1 Workers Do Not All Use Camera FPS

In the final two-D435 UVC16 profiles:

- representative 30 FPS has 21 modeled steady workers;
- stress 60 FPS has 23;
- equivalent camera instances with the same function and source signature
  share parameters;
- the application main thread remains `SCHED_OTHER`;
- each camera has a separately modeled `rs-wait-N` consumer;
- capture/synchronization follows approximately 30/60 FPS;
- maintenance includes approximately 91-ms and 910-ms families; and
- many event-driven/dormant entries use a conservative 4194.304-ms low-rate
  representation and the kernel's 100-us minimum runtime. They are not
  camera-period tasks.

Match runtime workers by creator signature and instance. Derive each
execution bound and period/minimum interarrival independently.

### 5.2 Deadline Construction and Validation

Formal profiles came from three 600-s `SCHED_OTHER` traces. For each logical
family, the largest logical-job execution across runs and equivalent camera
instances receives a 1.2 runtime factor. Period/deadline derives from the
shortest stable observed interval with additional margin.

| Workload | Entries/live workers | Total reserved utilization |
|---|---:|---:|
| Representative 30 FPS | 21/21 | 0.163794 CPU |
| Stress 60 FPS | 23/23 | 1.075842 CPU |

The first stress profile underestimated a roughly 0.91-s
`time_diff_keeper`. Its shared runtime increased from 333,930 ns to 608,753 ns,
`ceil(507,294 ns x 1.2)`, while period/deadline remained 910,646,585 ns.
After correction, a 10-minute steady gate recorded no runtime exhaustion or
SIGXCPU. Nine exhaustion events after `steady_state_end` occurred during stop
and destruction and were excluded by the predefined gate.

The evidence shows that low-rate maintenance workers must be modeled and that
teardown is a different phase from steady acquisition.

```text
tools/realsense_steady_bench/profiles/rpi5-6.12.96-rt-btf-uvc16-two-d435-representative-30fps.csv
tools/realsense_steady_bench/profiles/rpi5-6.12.96-rt-btf-uvc16-two-d435-stress-60fps.csv
```

## 6. Strongest Formal Result: PREEMPT_RT plus UVC16

### 6.1 `SCHED_OTHER` Calibration Freshness

| Workload | Runs | Observed/unique records per run | Duplicate/gap/stale/timeout/drop | UVC resubmit warning |
|---|---:|---:|---:|---:|
| Representative 30 FPS | 3 x 600 s | 71,946 | All zero | 0 |
| Stress 60 FPS | 3 x 600 s | 285,732--285,740 | All zero | 0 |

### 6.2 Formal `SCHED_DEADLINE` Validation

The representative runs produced 71,950, 71,946, and 71,944 unique records,
zero freshness/error metrics, and successful assignment of all 21 workers.

| Stress repetition | Observed | Unique | Duplicate | Gaps | Stale | Timeout/drop | UVC warning | Max / p99 interval |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 285,732 | 285,732 | 0 | 0 | 0 | 0 / 0 | 0 | 17.301 / 16.963 ms |
| 2 | 285,736 | 285,736 | 0 | 0 | 0 | 0 / 0 | 1 | 17.367 / 17.038 ms |
| 3 | 285,732 | 285,732 | 0 | 0 | 0 | 0 / 0 | 0 | 17.375 / 16.927 ms |

Every run succeeded on attempt one, assigned all 23 workers, and required no
recovery. The one `Failed to resubmit video URB (-1)` had no application-
visible corruption. `-1` is `-EPERM`, commonly associated with an URB poisoned
during stream stop. Because printk and probe clocks were not explicitly
correlated, state only that no corruption accompanied it, not that it was
definitely outside the steady gate.

### 6.3 OTHER versus Deadline Timing

On the same RT/UVC16, two-D435, no-interference platform, each cell is
3 x 600 s:

| Workload / policy | Mean | p99 | p999 | Maximum | Standard deviation |
|---|---:|---:|---:|---:|---:|
| Representative / OTHER | 33.358 ms | 33.809 ms | 34.383 ms | 35.898 ms | 0.154 ms |
| Representative / DEADLINE | 33.357 ms | 33.433 ms | 33.474 ms | 33.574 ms | 0.030 ms |
| Stress / OTHER | 16.799 ms | 18.578 ms | 19.737 ms | 21.622 ms | 0.656 ms |
| Stress / DEADLINE | 16.799 ms | 16.976 ms | 17.064 ms | 17.348 ms | 0.069 ms |

Deadline preserved mean rate while reducing the 30-FPS maximum by 2.324 ms
and the 60-FPS maximum by 4.274 ms. Relative to the mean, p99 excess fell by
approximately 83% and 90%. Both policies had zero freshness errors without
interference. This is tail tightening, not increased throughput or a no-noise
correctness difference.

```text
results/rt_uvc16/UVC16_RT_VALIDATION_SUMMARY.md
```

## 7. Historical IRQ-Isolation Pilot

On PREEMPT_RT, the two xHCI IRQ tasks were observed as `SCHED_FIFO:50`. The
historical topology assigned them to CPU0 and application/librealsense/noise
to CPU1--3.

In a short two-camera 60-FPS pilot without isolation, three runs produced
2/2, 103/119, and 7/7 duplicate/gap counts. Three isolated 20-s runs had zero
duplicates, gaps, timeouts, and Deadline overruns.

This justified treating IRQ topology as an experimental control at that time,
but 20 s does not prove that isolation universally eliminates loss. Later
paper treatments replaced isolation with explicit xHCI policy configuration;
do not present this historical pilot as the final host-path design.

```text
results/rpi5/deadline_irq_isolation_20260804/README.md
```

## 8. Memory Contention

### 8.1 Camera Traffic Estimate

The two-camera stress application payload is approximately 364 MiB/s and the
estimated USB input is 305 MiB/s. Including USB DMA writes, depth/IR reads,
color-conversion reads, and RGB writes gives a minimum DRAM-traffic estimate
of approximately 788 MiB/s; the actual librealsense path likely needs
1--1.5 GiB/s. This is analytical, not a memory-controller measurement.

### 8.2 Rate-Limited Fixed-Copy Boundary

Three private-buffer copy workers used 64-MiB buffers and 1-MiB pacing units.

| Target | Achieved estimate | Repetitions/duration | Duplicate | Gaps | Partially stale | Interpretation |
|---:|---:|---:|---:|---:|---:|---|
| 0 | 0 | 3 x 60 s | 0/0/0 | 0/0/0 | 0/0/0 | Control |
| 500 MiB/s | 476--480 MiB/s | 3 x 60 s | 0/6/0 | 0/6/0 | 0/2/0 | Near baseline, not a zero-error guarantee |
| 1000 MiB/s | 945--962 MiB/s | 2 valid x 60 s | 3/1 | 3/1 | 1/1 | Detectable mild degradation |
| 2000 MiB/s | 1544--1816 MiB/s | 3 x 60 s | 523/27/288 | 571/27/292 | 273/14/164 | High-variance transition region |
| 2500 MiB/s | 2029 MiB/s | 1 x 30 s | 76 | 80 | 44 | Clear degradation |
| 3000 MiB/s | 2662 MiB/s | 1 x 30 s | 1,562 | 2,170 | 814 | Severe degradation |
| 4000 MiB/s | 3307 MiB/s | 1 x 30 s | 4,666 | 9,548 | 2,065 | Overload |

No nonzero level is proven strictly error-free. Approximately 0.48 GiB/s is a
near-baseline region; degradation becomes repeatable around 0.95 GiB/s;
1.5--2.0 GiB/s is a variable transition; and a severe knee appears between
roughly 2.0 and 2.66 GiB/s.

Larger Deadline runtime reduced CBS overrun signals but did not eliminate
duplicates and gaps. Severe pressure therefore includes a shared DRAM/DMA
bottleneck that userspace CPU budget alone cannot repair.

This boundary used PREEMPT_RT/UVC5 and mostly 30/60-s points. It is mechanism
and boundary evidence, not a final UVC16 main figure.

```text
results/rpi5/memory_bandwidth_boundary_20260804/README.md
```

## 9. Historical CPU and GPU Interference

The old RT/UVC5 10-minute matrix included register-only CPU loops and
MobileNetV2 Vulkan GPU interference. Aggregated over three runs:

| Interference / policy | Duplicate | Gaps | Mean p99 interval | Worst maximum |
|---|---:|---:|---:|---:|
| CPU / OTHER | 23 | 1,603 | 26.387 ms | 134.450 ms |
| CPU / old DEADLINE | 44 | 212 | 16.930 ms | 136.632 ms |
| GPU / OTHER | 50 | 102 | 19.318 ms | 59.839 ms |
| GPU / old DEADLINE | 36 | 192 | 17.003 ms | 129.856 ms |
| Memory 2000 / OTHER | 0 | 100 | 19.086 ms | 88.503 ms |
| Memory 2000 / old DEADLINE | 582 | 788 | 17.292 ms | 138.591 ms |

CPU interference strongly affected OTHER tails, while Deadline retained most
frame-period behavior. GPU impact was smaller. Memory freshness was more
complex and could not be corrected by CPU scheduling alone.

The old Deadline profile later proved to underestimate low-rate and burst
execution, and this kernel used UVC5. Treat these data as discovery and trend
evidence, not as the final UVC16 Deadline statistics. The suite also contains
two 3600-s runs and all four policy conditions under both workloads.

```text
results/rpi5/rtns_formal_12h_20260804/
```

## 10. Locating a Freshness Failure in UVC/V4L2

A standard/UVC5, two-D435, representative, OTHER 600-s trace reproduced one
depth freshness failure on each camera:

| Camera | Missing depth sequence | Received bytes | Expected bytes | Completeness | UVC ERR | Invalid header | xHCI URB error |
|---|---:|---:|---:|---:|---:|---:|---:|
| `327122075717` | 12400 | 246,536 | 814,080 | 30.284% | 0 | 0 | 0 |
| `948122073863` | 16903 | 738,056 | 814,080 | 90.661% | 0 | 0 | 0 |

The event chain was:

```text
only part of the depth payload exists at the UVC frame boundary
    -> uvc_video_validate_buffer() marks the buffer erroneous
    -> V4L2 does not deliver the corrupted buffer as a valid frame
    -> the next valid depth frame exposes a sequence gap
    -> the syncer pairs a new other stream with the last valid depth frame
    -> the application observes duplicate depth / partial staleness
```

The trace excludes probe-created duplicates, loss of a depth frame already
dequeued normally by librealsense, application V4L2 queue starvation, UVC
header ERR, invalid header, host-reported isochronous packet error, xHCI error
giveback, autosuspend, and startup failure.

The strongest statement is: **at the next frame boundary, the UVC layer had a
successful-looking but incomplete depth payload.** The trace cannot separate
early termination by the camera, physical-link loss, or lower host-path loss
reported as success. It does not prove camera-firmware or xHCI fault.

```text
results/freshness_path_diagnostics/FINDINGS.md
```

## 11. `UVC_URBS=5` versus 16

### 11.1 Preliminary Controlled Comparison

Under standard PREEMPT 6.12.96, the same two-D435 topology, representative
30 FPS, OTHER, and 1500 MHz:

| Kernel | Runs | Validated UVC frames | Short/corrupted depth | Raw gaps | xHCI error givebacks |
|---|---:|---:|---:|---:|---:|
| UVC5 | 2 | 146,415 | 3 | 3 | 0 |
| UVC16 | 3 | 219,627 | 0 | 0 | 0 |

A sixteen-request pool is a promising host receive-pipeline mitigation. It
adds margin for completion, copying, IRQ scheduling, and resubmission delay;
it is not a sixteen-frame application cache and does not increase bus
bandwidth.

The UVC5 baseline observed only three rare failures in two runs, so confidence
intervals overlap. Supported wording is that UVC16 eliminated the corruptions
observed in this validation and is consistent with mitigation of host request
starvation. It is not yet a proof that 5-to-16 is the unique cause or that
UVC16 never loses data.

```text
results/freshness_path_diagnostics/UVC16_COMPARISON.md
docu/realsense_depth_frame_loss_and_uvc_urbs.md
```

## 12. Historical Standard versus PREEMPT_RT Match

At commit `e51e77e`, UVC5, two D435, the then-current CPU/IRQ topology,
1500 MHz, and no interference, both kernels completed two workloads and four
policies with 3 x 600 s per cell. The table excludes superseded old Deadline:

| Workload | Policy | RT duplicate/gap | Standard duplicate/gap |
|---|---|---:|---:|
| Representative | OTHER | 0 / 0 | 49 / 65 |
| Representative | RR-RM | 0 / 0 | 55 / 71 |
| Representative | FIFO-RM | 0 / 0 | 47 / 61 |
| Stress | OTHER | 19 / 71 | 256 / 340 |
| Stress | RR-RM | 33 / 117 | 275 / 399 |
| Stress | FIFO-RM | 73 / 141 | 307 / 415 |

These data associate PREEMPT_RT/UVC5 with fewer freshness failures but do not
attribute all improvement to preemption. Incomplete depth events are rare and
random, the final RT stack later changed to UVC16, and the matched standard
stack remained UVC5. Timing was not monotonically better on RT either: mean
stress/OTHER p99 was 17.962 ms on RT and 17.404 ms on standard.

The supported statement is that matched UVC5 data associate PREEMPT_RT with
fewer freshness failures without showing universal timing dominance. A strong
kernel-preemption RQ requires a final-stack standard/UVC16 control.

```text
results/rpi5/rtns_formal_12h_20260804/data/control_representative/
results/rpi5/rtns_formal_12h_20260804/data/control_stress/
results/rpi5/rtns_standard_matched_20260805/data/formal_representative/
results/rpi5/rtns_standard_matched_20260805/data/formal_stress/
```

## 13. Paper-Ready Conclusions

| Conclusion | Evidence | Strength and wording |
|---|---|---|
| Librealsense is a phase-dependent multithreaded workload, not one frame-period task | E2, source, steady profiles | Strong; core contribution |
| Startup, first frameset, steady convergence, and teardown are distinct | E2 and Deadline gate | Strong; do not use `pipeline::start()` return as startup completion |
| This D435 four-stream startup repeatedly has a 22-role modal shape | E2, 20/20 | Strong within the stated x86/backend/workload scope |
| First-frame wait dominates startup | E2 | Strong: median 516.843 ms versus 39.389-ms start call |
| Steady workers have heterogeneous rates | E11/E12 | Strong: frame, approximately 91 ms, approximately 910 ms, and event-driven families |
| Trace-derived per-family Deadline tightens delivery tails | E12 | Strong: same mean, maximum reduced by 2.324/4.274 ms at 30/60 FPS |
| Corrected profile has no exhaustion inside the steady gate | E12 | Strong; exclude teardown exhaustion |
| IRQ placement was an important experimental control | E5 | Moderate historical evidence; do not claim universal elimination |
| Memory contention causes duplicates, gaps, and stale framesets | E8 | Strong mechanism evidence with a probabilistic boundary |
| More userspace runtime cannot repair severe memory/DMA contention | E5/E8 | Strong qualitative evidence |
| Successful `wait_for_frames()` does not guarantee stream freshness | E6/E8/E9 | Strong; report freshness metrics |
| Diagnosed depth failure follows an incomplete UVC buffer path | E9 | Strong diagnosis; lower physical cause remains unknown |
| Final RT/UVC16 stack had no observed corruption in current calibration/formal runs | E11/E12 | Strong observational result, not a zero-failure proof |
| UVC16 is more promising than UVC5 for incomplete depth events | E10 | Moderate; needs larger randomized A/B for strong causality |
| PREEMPT_RT does not improve every timing metric automatically | E7 | Moderate; final-stack match desirable |

## 14. Claims Not Established

The evidence does not prove:

1. UVC16 eliminates every D435 loss.
2. Camera firmware necessarily under-transmits an incomplete depth payload.
3. Raspberry Pi xHCI necessarily drops that payload.
4. PREEMPT_RT dominates the standard kernel under every workload and policy.
5. `SCHED_DEADLINE` dominates RR/FIFO/OTHER under every interference; the
   final UVC16 interference matrix was not complete at this update.
6. The system scales to three or more cameras; formal evidence used two D435.
7. A D435 profile transfers directly to D415/D455.
8. Zero errors in 600 s imply zero failure probability.
9. Every absolute execution time is tracer-free WCET; instrumentation overhead
   required independent validation.

## 15. Data Selection for the Paper

### 15.1 Main Text

1. `startup_model_other_20runs_20260723` for startup structure and timeline.
2. `results/rt_uvc16/UVC16_RT_VALIDATION_SUMMARY.md` for final no-interference
   OTHER-versus-Deadline timing and freshness.
3. `results/freshness_path_diagnostics/FINDINGS.md` for the path from
   application freshness failure to incomplete UVC payload.
4. `results/rpi5/memory_bandwidth_boundary_20260804/README.md` for contention
   onset/knee, labeled as UVC5 diagnostic evidence.
5. `results/freshness_path_diagnostics/UVC16_COMPARISON.md` for the UVC16
   mitigation pilot with conservative wording.

### 15.2 Background, Ablation, or Appendix

1. `results/rpi5/rtns_formal_12h_20260804/`: complete historical UVC5 matrix,
   but old Deadline was superseded.
2. `results/rpi5/rtns_standard_matched_20260805/`: matched kernel evidence that
   does not exactly match final UVC16.
3. `results/rpi5/deadline_irq_isolation_20260804/`: rationale for the historical
   IRQ topology.
4. Pi startup policy data: platform variability rather than final causality.

### 15.3 Exclude from Final Statistics

- files or campaigns named `smoke`;
- interrupted campaigns or runs without a complete summary;
- failed recovery attempts;
- old schemas that counted every API return as a new frame;
- runs with unknown CPU frequency, autosuspend, or IRQ state; and
- old Deadline results presented as the final Deadline treatment.

## 16. Recommended Figures and Tables

1. Horizontal startup timeline from `startup_timeline.svg`.
2. Startup worker table grouped into startup-only, event-driven, and periodic
   families, with the full 22-role table in the artifact.
3. OTHER-versus-Deadline interarrival distribution at 30/60 FPS emphasizing
   p99 and maximum rather than mean alone.
4. Memory-boundary curve with achieved MiB/s on x and duplicates, gaps,
   partial staleness, and p99 interval on y.
5. Cross-layer failure diagram from incomplete UVC payload through V4L2 error,
   sequence gap, stale synchronization, and application duplicate.
6. UVC5/UVC16 pilot table that exposes run count and exposure rather than an
   overconfident causal bar chart.
7. Compact Deadline table containing family, multiplicity, runtime,
   period/deadline, and utilization rather than full symbol stacks.

## 17. Highest-Priority Missing Data at This Update

1. **Final UVC16 interference matrix:** RT/UVC16, two workloads, OTHER,
   RR-RM, FIFO-RM, tuned Deadline, and at least none/CPU/memory interference,
   3 x 600 s.
2. **Standard/UVC16 matched control:** two workloads, four policies,
   3 x 600 s, to close the preemption RQ.
3. **Randomized/interleaved UVC5 versus UVC16 A/B:** at least 5--10 x 600 s per
   side for a stronger UVC16 causal claim.
4. **D455 1/2/3 scaling:** common workload and OTHER/Deadline first.
5. **Instrumentation overhead:** matched trace-on/off runs reporting CPU,
   latency tails, receive-path latency, and freshness.

Timerlat, USB-stick interference, Shi--Tomasi, and broad 60-minute matrices
were deferred. The existing evidence was already sufficient to draft the
worker model, startup analysis, methodology, no-interference Deadline result,
memory-interference mechanism, and UVC failure-path diagnosis.
