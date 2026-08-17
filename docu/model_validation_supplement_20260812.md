# Supplementary Model-Validation Experiments (2026-08-12)

## 1. Motivation

The existing data already include:

- 20 independent-process start-phase structure experiments;
- three 600 s thread-model calibrations for both representative 30 FPS and
  stress 60 FPS;
- a 192-run main matrix spanning two Linux 6.12 kernels, two workloads, four
  interference conditions, four scheduling policies, and three repetitions;
- a complete userspace-policy by xHCI-IRQ-policy factorial without
  interference.

The supplementary experiment therefore does not repeat the main matrix. It
answers two questions that had not yet been strictly controlled.

### Q1: Does LiME scheduler tracing significantly change the observed system?

The thread model obtains CPU execution and activation intervals from LiME. If
tracing itself substantially delays frames or creates freshness failures, the
model might describe the measurement tool. Only the LiME setting changes in
this experiment. The lightweight pthread lifecycle interposer remains enabled
in both groups so that thread identity records and binary layout are the same.

### Q2: Under CPU saturation, is promoting only librealsense userspace workers sufficient?

The temporal graph places xHCI IRQ service before userspace capture workers.
The experiment compares:

1. userspace `SCHED_OTHER`, xHCI IRQ `SCHED_OTHER`;
2. role-aware userspace `SCHED_FIFO` with rate-monotonic priorities, xHCI IRQ
   `SCHED_OTHER`;
3. role-aware userspace `SCHED_FIFO` with rate-monotonic priorities, xHCI IRQ
   `SCHED_FIFO/90`.

If the second treatment cannot consistently match the third, the USB IRQ
predecessor does not benefit automatically from a userspace priority increase
and must be configured jointly.

## 2. Fixed Conditions

- Raspberry Pi 5, CPU fixed at 1.5 GHz;
- two D435 cameras with librealsense serials `327122075717` and
  `948122073863`;
- cameras placed on separate SuperSpeed xHCI controllers;
- V4L2 with `uvcvideo` and `UVC_URBS=16`;
- `RelWithDebInfo`, corresponding to `-O2 -g -DNDEBUG`;
- no CPU isolation, `taskset`, or probe CPU affinity;
- autosuspend globally disabled and `power/control=on` verified per device;
- full reset of both selected D435 cameras before each logical run;
- three 30 s measurements per cell;
- Linux 6.12 standard PREEMPT with `threadirqs` and Linux 6.12 PREEMPT_RT;
- representative 30 FPS and stress 60 FPS workloads;
- four register-only CPU busy-loop workers, started after warm-up.

A D455 was attached to the same powered hub during the experiment but was not
opened by the probe. USB topology and device power state are archived so that
the experiment cannot be mistaken for a four-camera run.

## 3. Results

All 20 cells and 60 30 s runs completed successfully. The table reports the
median of three per-run p99 and max values. Duplicate, gap, and stale counts
are summed across the three runs.

### 3.1 Incremental LiME Overhead

| Kernel | Workload | LiME | p99 (ms) | max (ms) | Duplicate/gap/stale | Raw LiME data |
|---|---|---:|---:|---:|---:|---:|
| standard | representative | off | 33.486 | 34.843 | 0/0/0 | 0 MiB |
| standard | representative | on | 33.508 | 34.897 | 0/0/0 | 15.5 MiB |
| standard | stress | off | 17.772 | 19.545 | 0/0/0 | 0 MiB |
| standard | stress | on | 18.038 | 21.042 | 0/0/0 | 46.1 MiB |
| PREEMPT_RT | representative | off | 33.539 | 35.003 | 0/0/0 | 0 MiB |
| PREEMPT_RT | representative | on | 33.473 | 35.320 | 0/0/0 | 21.9 MiB |
| PREEMPT_RT | stress | off | 17.904 | 21.314 | 0/0/0 | 0 MiB |
| PREEMPT_RT | stress | on | 18.504 | 22.568 | 0/0/0 | 82.6 MiB |

For the representative workload, LiME changes p99 by `+0.07%` on standard
Linux and `-0.20%` on PREEMPT_RT, which lies within run-to-run variation. For
the stress workload, the changes are `+1.50%` and `+3.35%`, while max rises by
`7.66%` and `5.88%`. None of the 24 A/B runs has a freshness error.

LiME therefore does not create the duplicate, gap, or stale failures studied
in the paper and does not change the worker families. It does produce a small,
measurable perturbation in the 60-FPS tail. The paper must report this overhead
rather than claim that tracing is free.

### 3.2 Coordinated xHCI Scheduling under CPU Interference

| Kernel | Workload | Userspace/xHCI | p99 (ms) | max (ms) | Duplicate/gap/stale |
|---|---|---|---:|---:|---:|
| standard | representative | OTHER/OTHER | 36.108 | 40.755 | 0/0/0 |
| standard | representative | FIFO-RM/OTHER | 33.483 | 35.209 | 0/0/0 |
| standard | representative | FIFO-RM/FIFO90 | 33.469 | 33.576 | 0/0/0 |
| standard | stress | OTHER/OTHER | 21.019 | 27.180 | 1/33/0 |
| standard | stress | FIFO-RM/OTHER | 17.688 | 19.809 | 0/0/0 |
| standard | stress | FIFO-RM/FIFO90 | 17.937 | 18.268 | 0/0/0 |
| PREEMPT_RT | representative | OTHER/OTHER | 36.399 | 61.190 | 0/4/0 |
| PREEMPT_RT | representative | FIFO-RM/OTHER | 33.457 | 35.226 | 0/0/0 |
| PREEMPT_RT | representative | FIFO-RM/FIFO90 | 33.444 | 34.525 | 0/0/0 |
| PREEMPT_RT | stress | OTHER/OTHER | 22.295 | 51.809 | 0/34/0 |
| PREEMPT_RT | stress | FIFO-RM/OTHER | 18.040 | 53.193 | 12/23/0 |
| PREEMPT_RT | stress | FIFO-RM/FIFO90 | 16.979 | 17.465 | 0/0/0 |

From OTHER/OTHER to FIFO-RM/FIFO90:

- standard representative: p99 decreases by 7.3% and max by 17.6%;
- standard stress: p99 decreases by 14.7%, max by 32.8%, and freshness errors
  fall to zero;
- PREEMPT_RT representative: p99 decreases by 8.1%, max by 43.6%, and gaps
  fall to zero;
- PREEMPT_RT stress: p99 decreases by 23.8%, max by 66.3%, and gaps fall to
  zero.

PREEMPT_RT stress most clearly separates userspace from its kernel
predecessor. With FIFO-RM/OTHER, one camera in run 1 has 12 duplicates and six
gaps; the two cameras in run 2 have 17 gaps in total; run 3 has no error. After
xHCI IRQ service is changed to FIFO/90, all cameras in all three runs have zero
duplicates and gaps, while max falls from 53.193 to 17.465 ms, a 67.2%
reduction.

FIFO-RM/OTHER already eliminates freshness errors under standard-kernel
stress, so xHCI/FIFO90 does not further improve median p99. It still reduces
median max from 19.809 to 18.268 ms. The evidence supports a bounded claim:
coordinated xHCI priority mainly protects the receive-path tail and is a
necessary component for eliminating the residual freshness failure under
PREEMPT_RT stress. It does not establish that every percentile of every
workload must improve strictly.

The raw results were completely backed up from the Pi at:

```text
results/rpi5/model_validation_supplement_20260812
```

The backup is 349 MiB, and a checksum dry run found no difference between the
remote and local trees. The summary can be regenerated with:

```text
python3 tools/realsense_steady_bench/.analyze_model_validation_supplement_20260812.py \
  results/rpi5/model_validation_supplement_20260812
```

## 4. Scope of Interpretation

- Thirty-second experiments compare latency distributions and frequent
  interference effects but cannot prove the probability of rare failures.
- LiME on/off differences are incremental overhead for this platform,
  workload, and trace configuration.
- The xHCI experiment demonstrates a host receive-path scheduling dependency;
  it does not explain camera-firmware behavior.
- The pthread interposer remains present in the LiME-off group, so these data
  cannot establish zero total instrumentation overhead.
