# Stream-Composition Ablation of the Resource Model (2026-08-12)

## 1. Question

The resource model states that a camera stream profile changes more than a
nominal FPS value. Adding a stream changes:

- USB payload;
- memory byte touches from DMA, copying, and color conversion;
- the frame-driven librealsense families that must activate;
- actual CPU running and ready demand.

This experiment fixes camera, FPS, resolution, CPU frequency, policy, and USB
topology and changes only the enabled streams. It tests whether these
quantities move in the direction predicted by the model.

## 2. Experimental Design

- Platform: Raspberry Pi 5, CPU fixed at 1.5 GHz;
- camera: one D435, serial `327122075717`;
- backend: V4L2 with `uvcvideo` and `UVC_URBS=16`;
- kernels: Linux 6.12 standard and PREEMPT_RT;
- policy: userspace and xHCI IRQ service remain under `SCHED_OTHER`;
- no CPU affinity, `taskset`, or external interference;
- three 30 s steady-state measurements per cell;
- full reset, disabled RealSense autosuspend, and page-cache drop before every
  run;
- LiME and the pthread lifecycle interposer enabled throughout.

All three stream compositions use 60 FPS:

| Label | Enabled streams |
|---|---|
| Depth | Depth 848x480 Z16 |
| Depth+Color | Depth 848x480 Z16; Color 960x540 RGB8, with YUYV on the USB wire |
| All | The preceding streams plus IR1 and IR2 at 848x480 Y8 |

The single-camera all-stream data are reused from the same-day camera-scaling
experiment to avoid recollecting an identical condition.

## 3. Analytical Lower Bounds

USB payload is computed from the wire format:

```text
B_usb = FPS * sum(width * height * wire_bytes_per_pixel)
```

The memory byte-touch lower bound counts one V4L2 DMA write, one CPU read, and
one SDK frame write. Color adds a YUYV read and an RGB8 write:

```text
B_mem = 3 * B_usb + B_color_yuyv + B_color_rgb8
```

| Streams | USB payload | Memory byte-touch lower bound |
|---|---:|---:|
| Depth | 46.582 MiB/s | 139.746 MiB/s |
| Depth+Color | 105.908 MiB/s | 466.040 MiB/s |
| All | 152.490 MiB/s | 605.786 MiB/s |

These values are neither USB line rate nor memory-controller counters. They
are minimum payload and code-path byte touches required by the identified
data path. They help explain workload changes but are not strict admission
bounds.

## 4. Results

All 18 observations, comprising 12 newly collected runs and six reused
all-stream runs, succeed without a duplicate, sequence gap, stale frameset,
or measurement timeout.

The table reports medians across three runs. The max column is also the median
of three per-run maxima rather than the global maximum of pooled samples.

| Kernel | Streams | Threads | Frame families | Running cores | Ready cores | Delivery p99 / max |
|---|---|---:|---:|---:|---:|---:|
| Standard | Depth | 11 | 2 | 0.022 | 0.001 | 16.810 / 17.923 ms |
| Standard | Depth+Color | 12 | 3 | 0.130 | 0.003 | 16.978 / 19.025 ms |
| Standard | All | 13 | 4 | 0.211 | 0.005 | 17.127 / 19.163 ms |
| PREEMPT_RT | Depth | 11 | 2 | 0.023 | 0.002 | 16.821 / 18.185 ms |
| PREEMPT_RT | Depth+Color | 12 | 3 | 0.133 | 0.004 | 17.206 / 19.363 ms |
| PREEMPT_RT | All | 13 | 4 | 0.215 | 0.015 | 17.263 / 19.606 ms |

Running-demand ratios relative to Depth-only are:

| Kernel | Depth+Color | All |
|---|---:|---:|
| Standard | 5.89x | 9.60x |
| PREEMPT_RT | 5.67x | 9.21x |

### 4.1 Interpretation

The results first validate the structural model. Adding Color increases the
total thread count and frame-driven family count by one. Adding IR1 and IR2
increases each by one again. The two IR streams do not mechanically create
two pthreads because the D435 Depth/IR sensor path can deliver multiple
streams through one capture/callback family. Instance counts must come from
the observed family graph rather than directly from the number of streams.

CPU demand grows much faster than thread count. Depth+Color has only 9% more
threads than Depth-only, but its running demand is 5.7--5.9x larger. Sources
include the additional color payload, copies, YUYV-to-RGB8 conversion,
synchronization, and frameset aggregation. After both IR streams are added,
the thread count is only 18% above Depth-only while running demand is
9.2--9.6x larger. Thus, `n_i` describes family instances and `C_i/T_i`
describes the work of each family. The two are not interchangeable.

The two kernels have similar running demand. This quantity mainly describes
the workload's CPU service requirement rather than a tail-latency advantage
of either kernel. PREEMPT_RT has higher ready demand in the all-stream cells,
but the sample is small and has no interference, so this experiment alone
does not support a strong kernel comparison.

## 5. Scope of the Conclusion

This ablation can validate:

- a monotonic relation between stream composition and analytical resource
  requirements;
- whether an added stream creates or activates an additional frame family;
- whether CPU running demand rises with data-path work.

It cannot independently prove that:

- hardware USB or DRAM utilization equals the analytical values;
- observed CPU demand must be strictly linear in payload;
- 30 s without a freshness error guarantees long-term reliability.

## 6. Data and Integrity

- Complete laptop backup:
  `results/rpi5/model_stream_ablation_20260812`;
- reused all-stream data:
  `results/rpi5/model_camera_scaling_20260812`;
- analysis script:
  `tools/realsense_steady_bench/.analyze_model_stream_ablation_20260812.py`.

After backup, `rsync --dry-run --checksum --itemize-changes` found no
difference from the Raspberry Pi source. The Pi was finally restored to the
standard UVC16 kernel, the temporary `threadirqs` command-line option was
restored, and the experimental service was disabled and stopped.
