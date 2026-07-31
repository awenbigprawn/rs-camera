# RealSense Timerlat Platform Characterization

This independent Benchkit campaign measures Linux IRQ-timer and real-time
thread wake-up latency with `rtla timerlat hist`. It complements, but does not
replace, the RealSense frame statistics and LiME scheduler traces.

Timerlat is an active probe. It creates periodic work on every selected CPU and
therefore perturbs the system being measured. These runs are stored separately
from the uninstrumented steady-state campaign and must not be treated as camera
performance baselines. Timerlat measures its own high-resolution timer IRQ; it
does not directly measure the D435/xHCI USB interrupt latency.

## Minimal RTNS matrix

`configs/minimal_matrix.json` defines four deliberately separated load cases:

| Case | Camera load | Injected noise | Interpretation |
| --- | --- | --- | --- |
| `idle` | none | none | platform timer-latency baseline |
| `cpu_busy_only` | none | four register-only SCHED_OTHER workers | isolated runnable CPU contention |
| `one_camera_representative` | one D435, depth and color at 30 FPS | none | representative camera load |
| `two_camera_stress` | two D435 pipelines, all four streams at 60 FPS | none | camera-driven USB, DMA, IRQ, memory, and thread pressure |

The formal matrix runs each case five times for five minutes under each kernel:

```text
2 kernels x 4 load cases x 5 repetitions x 5 minutes = 200 minutes
```

The kernels are separate boots, not a Benchkit Cartesian variable. Run the same
matrix once with the non-RT kernel and once with the PREEMPT_RT kernel. The
runner verifies that `--kernel-label` agrees with the running kernel and records
the full release/version string.

Fixed controls are:

- Raspberry Pi CPUs 0-3;
- CPU frequency locked to 1500 MHz for the campaign and restored afterward;
- Timerlat kernel threads with a 1 ms period and `SCHED_FIFO:95`;
- 10 seconds of Timerlat warm-up excluded from the histogram;
- 1 microsecond histogram buckets and 10,000 entries;
- V4L2/`uvcvideo` for camera loads;
- SCHED_OTHER for the camera process;
- filesystem cache drop before every attempt.

The CPU-only case is intentionally separate from the two-camera case. This
preserves causal interpretation instead of conflating scheduler pressure with
the camera's USB and memory traffic. A mixed GPU/CPU/camera extreme run may be
added later as a diagnostic validation, but it is not part of this minimal
matrix.

## Requirements

The running kernel must expose the Timerlat tracer, normally requiring
`CONFIG_OSNOISE_TRACER=y` and `CONFIG_TIMERLAT_TRACER=y`. The `rtla` executable,
`usbreset`, Benchkit environment, RealSense permissions, and the two explicit
camera serial numbers must be available.

The command-line options follow the Linux 6.12
[RTLA Timerlat histogram documentation](https://www.kernel.org/doc/html/v6.12/tools/rtla/rtla-timerlat-hist.html).

Check the host before running:

```sh
command -v rtla
sudo grep -w timerlat /sys/kernel/tracing/available_tracers
uname -a
cat /sys/kernel/realtime 2>/dev/null || true
```

Run `sudo -v` once so the non-interactive per-run commands can lock CPU
frequency, drop caches, invoke Timerlat, read the kernel log, and recover a
camera startup failure.

## Smoke test

The idle case needs no camera and is the fastest validation of RTLA, Benchkit,
the histogram parser, and CPU-frequency restoration:

```sh
sudo -v

.venv/bin/python tools/realsense_timerlat_bench/run_timerlat_campaign.py \
  --kernel-label linux_non_rt \
  --case idle \
  --duration-seconds 10 \
  --nb-runs 1 \
  --results-dir tools/realsense_timerlat_bench/results/idle_smoke
```

The `--duration-seconds` override is for smoke/debug runs. Formal data must use
the 300-second value committed in the matrix.

## Full non-RT campaign

Boot the Linux 6.12 non-RT kernel and run:

```sh
sudo -v

.venv/bin/python tools/realsense_timerlat_bench/run_timerlat_campaign.py \
  --kernel-label linux_non_rt \
  --serial CAMERA_SERIAL_1 \
  --serial CAMERA_SERIAL_2 \
  --results-dir tools/realsense_timerlat_bench/results/non_rt_5min_5rep
```

## Full PREEMPT_RT campaign

Reboot into the matching Linux 6.12 PREEMPT_RT kernel and run:

```sh
sudo -v

.venv/bin/python tools/realsense_timerlat_bench/run_timerlat_campaign.py \
  --kernel-label linux_preempt_rt \
  --serial CAMERA_SERIAL_1 \
  --serial CAMERA_SERIAL_2 \
  --results-dir tools/realsense_timerlat_bench/results/preempt_rt_5min_5rep
```

For camera cases the runner starts the steady probe first and waits for its
`steady_state_begin` phase marker before launching Timerlat. If either selected
camera fails before that barrier, the failed attempt is retained, both complete
D435 USB devices are reset, and only that logical run is retried. A failure
after the barrier is retained as a measured failure and is not retried.

## Results

Each attempt retains:

- raw `timerlat_hist.txt` and `timerlat_stderr.txt`;
- parsed per-CPU IRQ/thread count, min, average, maximum, p50, p99, p99.9,
  p99.99, and overflow status in `timerlat_histogram.json`;
- Timerlat command and CLOCK_BOOTTIME process interval;
- camera summary, raw frame events, lifecycle phase markers, and process state;
- CPU-noise artifacts for the CPU-only case;
- topology, interrupt, kernel-log, cache-drop, and CPU-frequency evidence;
- all failed/retried attempts and the selected attempt index.

The Benchkit CSV exposes global maximum IRQ/thread latency, the worst per-CPU
p99.9 values, overflow count, camera frame/drop/timeout totals, the actual
kernel identity, and artifact paths. Compare equal-duration repetitions; a
maximum is duration-dependent and should be reported alongside tail quantiles
and the complete histogram.
