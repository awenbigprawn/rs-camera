# Role-Instance Scaling with Camera Count (2026-08-12)

## 1. Question

The CPU model uses:

```text
U_cpu_obs = sum_i n_i * C_hat_i / T_hat_i
```

Here, `n_i` is the number of instances of a worker family. The model expects
per-camera frame-driven families to be replicated as cameras are added, while
process-wide and shared services need not be replicated. This experiment
compares a one-D435 trace with the existing two-D435 baseline to test that
structural assumption.

## 2. Design

- Linux 6.12 standard PREEMPT with `threadirqs`, and Linux 6.12 PREEMPT_RT;
- representative 30 FPS and stress 60 FPS workloads;
- `SCHED_OTHER` userspace workers and `SCHED_OTHER` xHCI IRQ service;
- V4L2 with UVC16, CPU fixed at 1.5 GHz, and no acquisition affinity,
  `taskset`, or CPU isolation;
- three 30 s LiME traces per cell;
- the one-camera cells opened only the directly connected D435
  `327122075717`;
- the two-camera cells reused the same-day D435+D435 LiME-enabled baseline.

At the beginning of the one-camera experiment, the standard-kernel command
line did not enable `threadirqs`. Those invalid attempts streamed
successfully, but their xHCI policy did not strictly match the two-camera
baseline, so they were excluded. The formal cells were rerun in a clean
directory after verifying `threadirqs` in the active `/proc/cmdline`.

## 3. Results

| Kernel | Workload | Cameras | Threads | Frame families | Running cores | Ready cores | Delivery p99 / max (ms) | Dup/gap/stale |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| standard | representative | 1 | 12 | 3 | 0.045 | 0.001 | 33.487 / 34.238 | 0/0/0 |
| standard | representative | 2 | 22 | 6 | 0.099 | 0.003 | 33.508 / 34.897 | 0/0/0 |
| standard | stress | 1 | 13 | 4 | 0.211 | 0.005 | 17.127 / 19.163 | 0/0/0 |
| standard | stress | 2 | 24 | 8 | 0.559 | 0.048 | 18.038 / 21.042 | 0/0/0 |
| PREEMPT_RT | representative | 1 | 12 | 3 | 0.045 | 0.001 | 33.443 / 34.037 | 0/0/0 |
| PREEMPT_RT | representative | 2 | 22 | 6 | 0.096 | 0.004 | 33.473 / 35.320 | 0/0/0 |
| PREEMPT_RT | stress | 1 | 13 | 4 | 0.215 | 0.015 | 17.263 / 19.606 | 0/0/0 |
| PREEMPT_RT | stress | 2 | 24 | 8 | 0.526 | 0.070 | 18.504 / 22.568 | 0/0/0 |

| Kernel | Workload | Thread-count ratio | Frame-family ratio | Running-core ratio |
|---|---|---:|---:|---:|
| standard | representative | 1.83x | 2.00x | 2.22x |
| standard | stress | 1.85x | 2.00x | 2.64x |
| PREEMPT_RT | representative | 1.83x | 2.00x | 2.12x |
| PREEMPT_RT | stress | 1.85x | 2.00x | 2.45x |

## 4. Interpretation

Frame-driven families exactly double in all four comparisons: from three to
six for the representative workload and from four to eight for the stress
workload. This supports counting `n_i` per camera and worker family.

The complete steady thread set does not double. The representative workload
grows from 12 to 22 threads and the stress workload from 13 to 24, giving
ratios of about 1.83--1.85. The main thread and some process-wide services are
not replicated for every camera. The entire process task set must therefore
not be multiplied by the camera count.

Actual userspace running demand grows by more than the linear factor of two,
reaching 2.12--2.64x. This does not invalidate `n_i`, because the formula does
not multiply a one-camera mean CPU value. It uses conservative `C_hat/T_hat`
values obtained across runs and equivalent instances under the target
topology. Two-camera concurrency can alter cache behavior, copying,
callbacks, USB completions, and scheduling. A one-camera mean is therefore
not an exact multi-camera predictor, and a multi-camera profile must still be
calibrated on the target topology.

All 24 comparison runs contained zero duplicates, zero gaps, and zero stale
framesets. The scaling measurements are therefore not artifacts of additional
work caused by a freshness failure.

## 5. Capture-Worker Residual Localization (2026-08-18)

### 5.1 Follow-up experiment

The camera-count experiment above showed that userspace CPU utilization grows
faster than the number of frame-driven worker instances.  A follow-up trace
localized this residual instead of treating it as an unexplained calibration
term.  The comparison used the representative 30 FPS workload, `SCHED_OTHER`,
a fixed 1.5 GHz CPU frequency, and three 30 s repetitions.  No CPU affinity,
`taskset`, CPU isolation, or xHCI housekeeping placement was applied; all four
CPUs remained available.  The one-camera cell opened D455 `311322304911`.  The
four-camera cell opened three D455 cameras and one D435.  Comparing instance 1
in both cells therefore follows the same D455 and the same Color and Depth
worker families.

Additional markers divide each V4L2 capture callback into metadata/DQBUF,
frame allocation, the copy from the V4L2 buffer into the librealsense frame
pool, buffer return, format conversion, synchronization, and frameset
aggregation.  Each marker interval was intersected with the corresponding
LiME `sched_switch` intervals.  Hardirq and softirq overlap was then removed,
so the stage values below are task CPU residency rather than wall-clock span.

| Worker and stage | One camera (us/frame) | Four cameras (us/frame) | Increase (us/frame) | Share of task-CPU increase |
|---|---:|---:|---:|---:|
| Color, complete activation | 1,132.1 | 1,367.4 | 235.3 | 100.0% |
| Color, frame copy | 167.3 | 291.4 | 124.0 | 52.7% |
| Color, format conversion excluding join | 838.9 | 872.9 | 34.0 | 14.5% |
| Color, synchronization and frameset join | 47.4 | 72.0 | 24.6 | 10.5% |
| Color, complete marked path | 1,097.5 | 1,310.3 | 212.9 | 90.5% |
| Depth, complete activation | 356.4 | 466.3 | 109.9 | 100.0% |
| Depth, frame copy | 253.8 | 322.2 | 68.3 | 62.1% |
| Depth, format conversion excluding join | 12.8 | 20.1 | 7.4 | 6.7% |
| Depth, synchronization and frameset join | 7.2 | 11.9 | 4.6 | 4.2% |
| Depth, complete marked path | 314.3 | 410.1 | 95.8 | 87.1% |

The same raw frame sizes were copied in both cells: 614,400 bytes for Color
YUYV and 814,080 bytes for Depth Z16.  The effective on-CPU copy rate fell
from 3.67 to 2.11 GB/s for Color and from 3.21 to 2.53 GB/s for Depth.  The
additional hardirq overlap was only 7.7 us per Color activation and 7.2 us per
Depth activation.  Allocation, metadata, and buffer return each accounted for
only a few additional microseconds.  The residual is therefore primarily
inside the memory-intensive frame-copy path, with smaller contributions from
Color conversion and the inline frameset join.  It is not mainly an IRQ
accounting artifact, a higher activation count, or frame-pool allocation.

All six selected steady-state attempts had zero duplicates, sequence gaps,
stale framesets, and timeouts.  One four-camera logical run needed a retry
because two devices did not re-enumerate after the pre-run reset; only its
successful second attempt entered the analysis.

### 5.2 Raspberry Pi 5 memory path and conclusion boundary

The Raspberry Pi 5 uses a BCM2712 with four Cortex-A76 cores.  Each core has a
64 KiB L1 data cache and a private 512 KiB L2 cache, while all four cores share
a 2 MiB L3 cache.  Main memory is attached through a 32-bit LPDDR4X-4267
interface with a published peak bandwidth of up to 17 GB/s.  RP1 contains the
two independent xHCI controllers.  RP1 transfers USB traffic to BCM2712 over a
PCIe 2.0 x4 link rated at 16 Gbit/s, and the xHCI DMA path writes camera data
into the same host-memory system used by the CPU.  These properties are
documented by the official
[BCM2712 processor description](https://www.raspberrypi.com/documentation/computers/processors.html#bcm2712)
and the
[RP1 peripheral specification](https://datasheets.raspberrypi.com/rp1/rp1-peripherals.pdf).

The measured slowdown does **not** prove that aggregate DRAM bandwidth is
exhausted.  The analytical four-camera representative workload touches about
666 MiB/s, well below the published 17 GB/s peak, although the analytical
value omits cache-line fills, write allocation, writeback, coherence traffic,
and other system users.  Camera work is also bursty: DMA completion, frame
copy, conversion, and callback work cluster around frame arrivals rather than
consuming their average rate uniformly over 33.3 ms.

Cache capacity is a more immediate constraint than total DRAM capacity.  One
representative raw Color plus Depth pair occupies about 1.36 MiB; the four
input pairs alone occupy about 5.45 MiB.  The copy destinations and the RGB8
conversion output enlarge the active working set further, while BCM2712 has
only 2 MiB of shared L3.  This comparison does not predict a miss rate, but it
shows why four streaming pipelines cannot retain all active frame data in the
shared cache even though the board has 8 GiB of DRAM.

The stage trace therefore supports the narrower hypothesis that the shared
memory hierarchy becomes less efficient as cameras are added.  Larger
simultaneous working sets can evict cache lines, compete for the shared L3 and
memory interconnect, and expose additional DRAM latency.  A thread stalled on
a cache miss is still scheduled and therefore contributes to LiME task CPU
residency.  Section 5.3 tests this hypothesis with per-thread PMU counters.

The complete follow-up artifacts are backed up at:

```text
results/rpi5/capture_residual_converter_formal_20260818
```

The reproducible decomposition is implemented in:

```text
tools/realsense_steady_bench/analyze_capture_residual.py
```

### 5.3 PMU distinction: additional work or memory-hierarchy stalls

A second six-run experiment repeated the same one-D455 and four-camera cells
without CPU affinity or isolation.  During each 30 s run, `perf stat` attached
to every existing probe TID for a 20 s interval after startup.  The Arm PMU
recorded instructions, cycles, backend-stall cycles, L1-data-cache loads and
misses, generic last-level-cache (LLC) loads and misses, and cache misses.  The
table compares the median of three runs for instance 1, which is the same
D455 in both cells.  Counts are normalized by the 600 nominal frame periods
in each 20 s PMU interval; the comparison does not assume that this is an
exact activation counter.

| Worker metric | One camera | Four cameras | Relative change |
|---|---:|---:|---:|
| Color instructions/frame | 2.296 M | 2.326 M | +1.3% |
| Color cycles/frame | 1.708 M | 1.907 M | +11.7% |
| Color CPI | 0.745 | 0.816 | +9.5% |
| Color backend-stall fraction | 42.2% | 47.0% | +4.8 percentage points |
| Color L1D miss fraction | 1.02% | 1.18% | +0.15 percentage points |
| Color LLC miss fraction | 51.2% | 67.8% | +16.6 percentage points |
| Depth instructions/frame | 0.169 M | 0.178 M | +5.8% |
| Depth cycles/frame | 0.435 M | 0.632 M | +45.1% |
| Depth CPI | 2.568 | 3.632 | +41.5% |
| Depth backend-stall fraction | 60.9% | 67.9% | +7.0 percentage points |
| Depth L1D miss fraction | 4.68% | 4.98% | +0.30 percentage points |
| Depth LLC miss fraction | 29.8% | 46.7% | +16.9 percentage points |

The instruction count changes only slightly, whereas cycles per frame and
CPI increase substantially.  Both capture families also spend a larger share
of cycles backend-stalled and show a higher fraction of loads missing the
cache levels represented by the generic PMU events.  This separates the main
residual from an alternative explanation in which four-camera operation
simply executes much more code.  It instead provides direct evidence that the
same capture path loses memory-hierarchy efficiency under the larger
concurrent working set.  This result agrees with the stage decomposition:
most added task residency occurs during the fixed-size frame copy.

The PMU result still does not prove saturation of the LPDDR4X controller.
Cache-capacity misses, interconnect queuing, coherence effects, and DRAM
latency can all increase backend stalls before aggregate DRAM bandwidth
reaches its advertised peak.  Moreover, eight events were multiplexed; the
minimum per-event running coverage was 74--75%, and `perf` scaled the counts.
An uncore or memory-controller bandwidth counter would be required to claim
DRAM-controller saturation.  The defensible conclusion is therefore
**cache/shared-memory-hierarchy contention**, not **insufficient total DRAM
bandwidth**.

The PMU campaign and derived CSV/JSON files are backed up at:

```text
results/rpi5/capture_residual_pmu_v3_20260818
```

The reproducible PMU collection and analysis are implemented in:

```text
tools/realsense_steady_bench/run_capture_residual_pmu.sh
tools/realsense_steady_bench/analyze_capture_residual_pmu.py
```

## 6. Scope of the Conclusion

- This experiment validates the role-instance structure. It does not
  establish a linear performance law for an arbitrary camera count.
- The physical USB topologies differ: the one-camera group used a directly
  connected port, while the two-camera group distributed cameras over two
  xHCI controllers.
- Thread classification uses measured periods and activation counts. It is an
  artifact rule, not a guarantee of the librealsense API.
- A 30 s run is suitable for confirming a recurring family structure, but it
  cannot cover rare long-term failures.

The complete one-camera results were backed up and verified by checksum at:

```text
results/rpi5/model_camera_scaling_20260812
```

Analysis command:

```text
python3 tools/realsense_steady_bench/.analyze_model_camera_scaling_20260812.py \
  results/rpi5/model_camera_scaling_20260812 \
  results/rpi5/model_validation_supplement_20260812
```
