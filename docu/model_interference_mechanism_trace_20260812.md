# Thread-Level Mechanism Validation of the Resource Model (2026-08-12)

## 1. Experimental Question

The paper's resource model distinguishes two sources of timing loss:

1. A thread is runnable but temporarily receives no CPU service. This is
   ready wait.
2. A thread is executing, but cache effects, memory copies, or shared-memory
   service lengthen the CPU burst itself. This appears as increased execution
   time.

The final frameset inter-delivery time alone cannot distinguish the two. This
experiment retains all LiME scheduling events and separately measures
`running_ms`, `ready_ms`, frame-driven execution p99, and ready time per
activation at p99.

## 2. Fixed Conditions

- Raspberry Pi 5 with the PREEMPT_RT UVC16 kernel;
- two D435 cameras on separate xHCI controllers;
- V4L2 with `uvcvideo`, CPU fixed at 1.5 GHz;
- userspace families and xHCI IRQ service under `SCHED_OTHER`;
- no acquisition affinity, `taskset`, or CPU isolation;
- three 30 s measurements per cell, with interference started after warm-up;
- a full reset of both cameras before each logical run;
- reuse of the LiME-enabled, interference-free baseline collected on the same
  day.

Interference consists of four register-only CPU workers, four rate-limited
fixed-copy workers with an aggregate 2,000 MiB/s target, and continuous
MobileNetV2 Vulkan inference.

## 3. Interpretation of Metrics

- `Userspace running` is the sum of `running_ms` over all probe and
  librealsense threads divided by measurement duration, in CPU-core
  equivalents. It measures CPU service actually received, not queued demand;
  it can decrease under severe starvation.
- `Userspace ready wait` is the sum of `ready_ms` over all threads divided by
  duration. Waits of different threads can overlap, so this is cumulative
  core-equivalent wait rather than one wall-clock latency.
- `Frame exec p99` is the largest execution p99 among frame-driven families in
  each run, followed by the median across three runs.
- `Frame ready p99` is computed in the same way from ready time per
  activation.
- Delivery p99/max and freshness counters come from the frameset-delivery
  probe.

## 4. Results

Latencies are medians of the three per-run statistics. Duplicate, gap, and
stale counts are summed across the three runs.

| Workload | Interference | Delivery p99 / max (ms) | Dup/gap/stale | Running (cores) | Ready wait | Frame exec p99 (ms) | Frame ready p99 (ms) |
|---|---|---:|---:|---:|---:|---:|---:|
| Representative 30 FPS | none | 33.473 / 35.320 | 0/0/0 | 0.096 | 0.004 | 1.335 | 0.084 |
| Representative 30 FPS | CPU4 | 36.443 / 43.499 | 0/0/0 | 0.104 | 0.117 | 1.452 | 3.983 |
| Representative 30 FPS | memory 2000 | 33.885 / 35.683 | 0/0/0 | 0.116 | 0.009 | 1.717 | 0.524 |
| Representative 30 FPS | GPU | 34.414 / 36.027 | 0/0/0 | 0.101 | 0.010 | 1.428 | 1.026 |
| Stress 60 FPS | none | 18.504 / 22.568 | 0/0/0 | 0.526 | 0.070 | 2.864 | 2.590 |
| Stress 60 FPS | CPU4 | 23.560 / 62.665 | 21/52/0 | 0.500 | 0.724 | 3.340 | 9.647 |
| Stress 60 FPS | memory 2000 | 21.214 / 31.248 | 12/16/0 | 0.700 | 0.375 | 4.027 | 5.995 |
| Stress 60 FPS | GPU | 18.691 / 23.019 | 0/0/0 | 0.531 | 0.093 | 2.931 | 2.450 |

Relative to the no-interference baseline:

| Workload | Interference | Running change | Ready-wait multiplier | Frame exec-p99 change | Frame ready-p99 multiplier |
|---|---|---:|---:|---:|---:|
| 30 FPS | CPU4 | +7.4% | 29.4x | +8.8% | 47.6x |
| 30 FPS | memory 2000 | +20.1% | 2.3x | +28.6% | 6.3x |
| 30 FPS | GPU | +5.1% | 2.5x | +7.0% | 12.3x |
| 60 FPS | CPU4 | -4.9% | 10.4x | +16.6% | 3.7x |
| 60 FPS | memory 2000 | +33.0% | 5.4x | +40.6% | 2.3x |
| 60 FPS | GPU | +0.8% | 1.3x | +2.3% | 0.9x |

## 5. Support for the Model

CPU-only interference primarily creates ready delay. At 30 FPS, cumulative
ready wait grows by 29.4x and frame-family ready p99 by 47.6x, while execution
p99 grows by only 8.8%. At 60 FPS, ready wait grows by 10.4x and 21 duplicates
and 52 gaps occur. Observed running service decreases by 4.9% because starved
threads receive less CPU. This decrease must not be interpreted as lower
workload CPU demand.

The rate-limited fixed-copy workload consumes both CPU and memory and cannot
be called a pure DRAM experiment. Its effect on execution is nevertheless
substantially stronger than that of CPU-only interference: execution p99
grows by 28.6% at 30 FPS and 40.6% at 60 FPS, while 60-FPS userspace running
grows by 33.0%. The result supports the claim that memory-copy contention can
lengthen scheduled bursts, while the workers' CPU use also contributes ready
delay.

GPU effects are lighter and workload dependent. At 30 FPS, execution p99
grows by 7.0% and ready p99 by 12.3x. At 60 FPS, the metrics remain close to
baseline and no freshness errors occur. GPU interference is therefore not a
stable, single DRAM-bandwidth effect. CPU cost from Vulkan submission, shared
memory access by V3D, and workload phasing can all contribute. Without memory
controller counters, the causal components cannot be separated further.

The CPU, USB, and byte-touch terms in `R(W)` therefore cannot be collapsed
into one average utilization. Scheduling delay and execution inflation alter
the capture path in different ways. Under sufficient pressure, frameset
freshness amplifies those delays into duplicates and gaps.

## 6. Limitations

- Thirty-second runs expose frequent interference effects but cannot estimate
  a rare long-term failure rate.
- The maximum across frame families is a mechanism indicator, not the WCET of
  every family.
- `ready_ms` is cumulative thread wait and cannot be added directly to
  delivery latency.
- Fixed-copy is not a hardware DRAM-bandwidth generator.
- The GPU cells have no V3D, interconnect, or DRAM hardware counters.
- Baseline and interference cells were collected in adjacent same-day
  campaigns rather than in run-by-run randomized order.

The complete results were verified and backed up at:

```text
results/rpi5/model_interference_trace_20260812
```

Analysis command:

```text
python3 tools/realsense_steady_bench/.analyze_model_interference_trace_20260812.py \
  results/rpi5/model_validation_supplement_20260812 \
  results/rpi5/model_interference_trace_20260812
```
