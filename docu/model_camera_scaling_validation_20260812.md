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

## 5. Scope of the Conclusion

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
