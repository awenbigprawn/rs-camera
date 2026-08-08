# RealSense Steady-State Benchmark

This benchmark characterizes librealsense after camera startup has completed. It
replaces the former `realsense_usb_topology_bench`, whose `camera_count` field
was only a label and whose probe could operate only one pipeline.

The new probe creates one `rs2::pipeline` per selected camera, waits until every
camera has completed its warm-up, emits one global `steady_state_begin` marker,
and then records the same number of frame deliveries from every camera.

## Campaign factors and fixed controls

| Role | Setting |
| --- | --- |
| Cartesian factors | workload case, scheduling policy, CPU-noise mode, memory-noise mode, GPU-noise mode, and USB-storage-noise mode |
| Operational inputs | camera serials, CPU/memory-noise worker counts and affinity, memory buffer size, frame override, repetitions, build jobs, and output path |
| Fixed controls | V4L2 backend, `uvcvideo`, 1500 MHz CPU, cache drop before each run, CPU 0 for housekeeping/xHCI IRQs, CPUs 1-3 for the benchmark, and RT priority 80 |

Fixed controls are declared once in
`tools/realsense_bench_common/settings.py`; `steady_settings.py` adds only
steady-specific paths and factor names. The controls are copied into the
Benchkit CSV and per-run manifest and are intentionally not command-line
factors.

## Python module layout

The campaign runner is split by responsibility:

- `run_steady_campaign.py` parses CLI arguments, selects cases, and constructs
  the Benchkit Cartesian-product campaign;
- `steady_benchmark.py` adapts one steady-state run to Benchkit;
- `steady_attempts.py` supplies the steady-specific failure classifier and
  artifact writer to the shared attempt engine;
- `noise_workloads.py` owns CPU, memory, USB-storage, and GPU noise processes;
- `steady_results.py` assembles the steady-specific result columns;
- `steady_settings.py` contains only steady paths and factor names;
- `../realsense_bench_common/` owns scheduling/trace command construction,
  retry orchestration, full-device recovery, CPU/RSUSB/kernel controls, cache
  dropping, cgroup-v2 CPU/IRQ isolation, common result fields, and old/new
  artifact-layout resolution.

## Measurements

The probe writes:

- every delivered frame's `CLOCK_BOOTTIME` timestamp;
- the camera, stream, stream index, frame number, sensor timestamp, and
  timestamp domain for each event;
- per-camera and per-stream frame counts and frame-number gaps;
- host and sensor inter-arrival distributions;
- `wait_for_frames()` blocking-time distributions in `wait` mode;
- pipeline start/stop times and the exact global measurement window.

The campaign combines these data with:

- pthread create/start/name/exit/join events from
  `tools/realsense_thread_trace/trace_pthreads.c`;
- scheduler state transitions collected by the unmodified LiME dependency;
- per-thread steady-state execution, ready, sleeping, response, and period
  distributions;
- before/after USB topology, autosuspend state, and xHCI/UVC interrupt
  snapshots;
- the requested Linux scheduling policy and fixed CPU frequency.

Raw frame events and tracing output are retained so paper figures can be
regenerated without rerunning the hardware experiment.

### Freshness and loss metrics

One return from `wait_for_frames()` is not necessarily one new camera
frameset. Under severe contention, librealsense can aggregate newly arrived
streams with cached frames from other streams, and the application can observe
the same frame number more than once. The legacy `frames`, `deliveries`, and
`drops` fields are retained for compatibility, but formal loss analysis uses
the following post-processed fields:

- `observed_frames`: all frame objects returned during measurement;
- `unique_frames`: distinct frame numbers per camera and stream;
- `duplicate_frames`: observations whose camera/stream/frame-number identity
  was already present in the measurement;
- `sequence_gaps`: missing frame numbers inside each stream's observed
  `[minimum, maximum]` range;
- `nonadvancing_frames`: observations whose frame number did not advance past
  that stream's previous high-water mark;
- `out_of_order_frames`: observations below the stream's previous high-water
  mark;
- `fully_fresh_framesets`: every child stream advanced;
- `partially_stale_framesets`: at least one child stream advanced and at least
  one did not; and
- `stale_framesets`: no child stream advanced.

These values are computed after `steady_state_end` from the raw events already
retained by the probe. No set lookup, sorting, or uniqueness analysis is added
to the measured `wait_for_frames()` path. `freshness_analysis_ms` records the
extra post-processing time, and `freshness_analysis_begin/end` trace markers
make that teardown work identifiable. The temporary analysis storage is one
64-bit frame-number vector per stream and is released when the summary has
been written.

### Freshness path diagnostics

Use `--freshness-kernel-trace` to localize duplicate frames and sequence gaps
without recording every USB transfer. The option implies `--v4l2-diagnostics`
and combines four clock-aligned layers:

- exceptional xHCI isochronous-IN URB givebacks;
- UVC frame completion, frame-length validation, corrupted-buffer, and
  empty-video-queue probes;
- native VB2 and V4L2 queue/dequeue tracepoints; and
- librealsense DQBUF, callback, sensor-dispatch, syncer, and application frame
  events.

The wrapper supports the archived Raspberry Pi 5 6.12.96 standard-BTF and
PREEMPT_RT-BTF kernels. It uses their BTF-verified UVC structure layout for the
fixed-offset dynamic probes. Every attempt produces
`freshness_kernel_trace.dat`, decoded kernel UVC/V4L2 CSV files,
`freshness_path_correlations.csv`, and
`freshness_kernel_trace_summary.json`. Each raw librealsense DQBUF gap is
classified as a device-set UVC payload error, host-reported isochronous packet
error, invalid payload header, short/overflowed UVC frame, UVC buffer
starvation, xHCI error, loss before UVC frame completion, loss between UVC and
V4L2 dequeue, or loss between the kernel dequeue and librealsense. The compact
validation event is recorded once per completed frame, but only exceptional
rows are retained in `kernel_freshness_exception_events.csv`.
Freshness mode also uses compact V4L2 post-processing: it keeps the raw binary
trace, summary, and sequence-gap CSV but omits the much larger full event and
duration CSV files. Other V4L2 timing and deadline-overrun runs retain those
detailed CSV outputs.

Example two-camera diagnosis:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/timing_trace_60s.json \
  --case representative_depth_color_30fps_60s_trace \
  --policies other \
  --freshness-kernel-trace \
  --measurement-duration-seconds 600 \
  --nb-runs 1 \
  --serial SERIAL_1 \
  --serial SERIAL_2 \
  --recover-on-failure full-reset \
  --max-attempts-per-run 3 \
  --results-dir tools/realsense_steady_bench/results/freshness_path
```

Do not enable `--overrun-kernel-trace` in the same run: both options own one
`trace-cmd` session. If the low-volume trace identifies unexplained loss before
UVC frame completion, use a separate short xHCI flight-recorder run. Recording
every successful isochronous URB for ten minutes is intentionally avoided
because its volume and overhead can perturb the camera workload.

## Probe Modes

`--delivery wait` gives every camera an explicitly named `rs-wait-N` acquisition
thread that calls `wait_for_frames()`. `--delivery callback` lets librealsense
invoke the application callback from its own dispatch path. Both modes use the
same warm-up barrier and measurement interval.

Stream modes are:

- `depth`: depth only;
- `depth_color`: depth and RGB;
- `stereo_all`: depth, RGB, infrared 1, and infrared 2 (`d435_all` is retained
  as a compatibility alias).

The default D435 all-stream profile is depth/IR at 848x480 and RGB at 640x480,
all at 30 frames/s.

Formal steady-state cases use a fixed wall-clock measurement duration. The
legacy `--frames` stop condition remains available for short functional tests,
but it must not be used to compare overloaded workloads: delayed deliveries
would otherwise make a nominal ten-minute run last longer. Use
`--measurement-duration-seconds` for a command-line override, or set
`probe.measurement_duration_ms` in a case configuration.

## Build

From the repository root:

```sh
cmake -S . -B build-realsense-steady -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build-realsense-steady --target realsense_steady_probe -j4
```

To build the selected Vulkan GPU-noise workload, initialize the pinned ncnn
submodule and install the optional dependencies:

```sh
./scripts/install_dependencies_ubuntu.sh --gpu-noise --build
```

## Short Smoke Campaign

The following takes approximately ten seconds per policy after startup:

```sh
sudo -v

.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --case one_camera_all_streams_wait \
  --policies other rr fifo \
  --frames 300 \
  --nb-runs 1 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/smoke
```

The runner locks CPU frequency once before the first run and restores dynamic
scaling after the entire campaign, including interrupted campaigns. The paper
campaign fixes `CAMPAIGN_CPU_FREQUENCY_MHZ = 1500` and
`CAMPAIGN_RT_PRIORITY = 80` near the top of the runner; neither is a Cartesian
factor or CLI override.

Before every attempt, including a retry of the same logical Benchkit run, the
runner executes `sync` and writes `3` to `/proc/sys/vm/drop_caches`. This
establishes a cold Linux page cache and reclaims dentries and inodes before noise
warm-up or camera startup; anonymous memory and swap are not cleared. The
operation is recorded in `memory_cleanup_before_run.json` and the campaign CSV.
Run `sudo -v` before the campaign. Cache cleanup is a fixed paper-campaign
control.

### Fixed CPU and xHCI IRQ isolation

The paper campaign enables CPU isolation by default. Immediately before the
first measured run, the runner:

1. verifies that CPU 0 and CPUs 1-3 exactly partition the online CPUs;
2. creates an isolated cgroup-v2 cpuset partition on CPUs 1-3;
3. discovers every connected D435's xHCI controller from USB sysfs;
4. resolves the corresponding IRQ numbers from `/proc/interrupts`;
5. pins those IRQs to CPU 0; and
6. moves the campaign process into the CPUs 1-3 partition.

All subsequently created probe, librealsense, LiME, and noise processes inherit
CPUs 1-3. CPU 0 remains available to xHCI IRQ threads and system housekeeping.
This is a campaign-wide fixed control, so every scheduling policy receives the
same three application CPUs. At normal completion, Python exception, or
keyboard interruption, cleanup restores each IRQ's exact prior affinity, moves
the runner back to its original systemd cgroup, removes the temporary
partition, and restores CPU frequency.

The active topology is written to `cpu_isolation_campaign.json`, copied into
the run manifest, and flattened into `cpu_isolation_*` CSV columns. IRQ numbers
are never hard-coded because they may change across kernels or boots. Use
`--no-cpu-isolation` only for diagnostics or unsupported machines. Alternative
platform layouts can be supplied as fixed operational controls, for example:

```sh
--housekeeping-cpus 0 --benchmark-cpus 1-3
```

## Multi-camera warm-up, recovery, and retry

The C++ probe uses a global warm-up barrier. It emits `steady_state_begin` and
starts the measured interval only after every selected camera has delivered its
configured number of warm-up frames. For two cameras, one healthy camera cannot
start the measurement while the other reports `Frame didn't arrive`.

When any noise condition is enabled, the runner uses a second barrier. The
probe first starts and warms every camera with no noise process running. It then
publishes `camera_warmup_ready`; the runner starts and warms the selected noise
workers, and finally publishes `measurement_start_gate`. Camera acquisition
continues while this gate is closed, but those deliveries are deliberately not
included in the formal measurement. Therefore memory pressure cannot prevent
USB enumeration or initial pipeline warm-up, and noise warm-up is also excluded
from the measured interval. The transition timestamps are recorded in
`noise_transition.json` and in the `transition_*` result columns.

Before measurement begins, a `wait_for_frames()` timeout is a startup or noise
transition failure and remains eligible for full-reset recovery. During a
fixed-duration measurement, the same timeout is counted and acquisition
continues until the wall-clock deadline. It therefore remains visible as a
performance failure without changing the duration or replacing the measured
run with a later retry.

Before attempt 1 of every logical run, the runner firmware-resets and
composite-USB-resets every selected camera. This mandatory-by-default baseline
reset occurs before pipeline startup and prevents previous runs from leaving
device or UVC state behind. `--no-reset-before-run` is available only for
diagnosis and should not be used for formal data.

Full-reset failure recovery is also enabled by default. If an attempt fails
before `steady_state_begin`, the runner:

1. preserves that failed attempt and its LiME, pthread, topology, kernel-log,
   noise, and probe artifacts under `attempt-N/`;
2. performs a librealsense firmware hardware reset for every selected camera;
3. resets the parent composite USB device for every camera, thereby resetting
   all Depth, RGB, and IR UVC interfaces together;
4. waits for every serial number to re-enumerate; and
5. repeats only the same logical Benchkit run, up to three attempts by default.

The reset controls are `--reset-before-run`/`--no-reset-before-run`,
`--recover-on-failure {none,full-reset}`, `--max-attempts-per-run`,
`--recovery-reset-timeout-ms`, `--recovery-wait-seconds`, and
`--recovery-settle-seconds`.

Every attempt remains under `attempt-N/`, including the selected successful
attempt. The run root contains `attempts.json`, `selected_attempt.txt`, a
logical-run summary, and a convenience stdout copy. The shared resolver also
understands historical campaigns in which the selected steady attempt was
promoted to the run root.

For a matrix that mixes camera counts, every CSV row reserves fields through
the largest configured camera index. A single-camera row therefore leaves the
`camera_1_*` fields empty instead of changing the header; subsequent
multi-camera rows retain correct column alignment. The per-attempt JSON remains
the authoritative raw record.

A failure after `steady_state_begin` is a measured steady-state outcome. The
runner resets all cameras to protect the following campaign point but does not
retry and conceal that failure.

Use `--no-lime` only for functional debugging. It keeps the pthread lifecycle
trace but cannot produce scheduler execution-time distributions.

## Per-thread SCHED_DEADLINE mode

The deadline policy is a two-stage policy. The process and every
camera/librealsense thread starts under SCHED_OTHER. After each camera has
completed the initial part of warm-up, the probe maps the live worker pthreads
to a previously generated temporal model and applies one reservation to every
matched worker with sched_setattr(). The process main thread remains
SCHED_OTHER because it is an aperiodic phase-control thread rather than a
frame-processing worker. The cameras continue warming under SCHED_DEADLINE, and
the probe emits steady_state_begin only after the complete warm-up interval.
Startup work is therefore not charged to the steady-state reservations and the
new policy has a stabilization interval before measured sampling.

Generate a profile from one or, preferably, several independent SCHED_OTHER
LiME attempts for exactly the same kernel, backend, probe build, camera count,
delivery mode, and stream workload:

~~~sh
.venv/bin/python \
  tools/realsense_steady_bench/generate_deadline_profile.py \
  --trace-run PATH_TO_OTHER_ATTEMPT_1 \
  --trace-run PATH_TO_OTHER_ATTEMPT_2 \
  --trace-run PATH_TO_OTHER_ATTEMPT_3 \
  --output tools/realsense_steady_bench/profiles/WORKLOAD.csv
~~~

The generator first merges burst activations into logical jobs. Workers with
the same ASLR-independent creation-stack signature are treated as instances of
one thread role, including corresponding workers belonging to different
cameras. It pools every calibration run and every live instance of that role.
For role \(r\), every instance receives the same conservative parameters:

~~~text
runtime_r  = max(kernel minimum, 1.20 * role-wide maximum logical-job execution)
deadline_r = period_r
period_r   = min(kernel maximum, 0.91 * role-wide minimum stable logical-job period)
~~~

Thus, a slower camera instance raises the reservation of every corresponding
camera worker rather than creating camera-specific reservations. Execution
fragments separated only by preemption remain in the same activation, and
sleep-separated burst activations belonging to one model period are summed.
The minimum period is taken only from the stable logical-period mode, not from a
raw micro-gap inside a burst. The accompanying WORKLOAD.csv.json records all
observations, clamping, formula constants, sources, and total reserved
utilization. These are empirical reservation candidates, not WCET guarantees.

### Stored validated profiles

`profiles/rpi5-6.12.96-standard-btf-two-d435-representative-30fps.csv` is the
validated profile for two D435 cameras running the 30 FPS representative
Depth+Color workload. Same-role workers use shared parameters across cameras.
After an initial 600-second validation exposed four budget exhaustions in one
`time_diff_keeper`, both instances of that role were raised to 372755 ns. A
second 600-second validation assigned all 21 workers and observed no runtime
exhaustion or SIGXCPU event inside the steady-state gate. The profile reserves
0.18787 CPU equivalents in total. Its build signatures require
`--v4l2-diagnostics-build-only` (or full `--v4l2-diagnostics`).

`profiles/rpi5-6.12.96-standard-btf-two-d435-stress-60fps.csv` is the stored
steady-state profile for the Raspberry Pi 5 standard BTF kernel, two D435
cameras, and the 60 FPS all-stream stress workload. It was validated for one
600-second steady window with all 23 workers assigned, no measurement timeout,
and every observed steady-state activation below its runtime reservation. Its
total reserved utilization is 1.29497 CPU equivalents, or 43.17 percent of the
three-CPU benchmark partition. The sidecar JSON records the exact platform,
workload, derivation, checksums, and validation scope.

The validation process reported two SIGXCPU events after `steady_state_end`
during camera stop and resource destruction. Teardown is explicitly outside
this steady-state profile's scope. Do not reuse this profile with a different
kernel, librealsense build, camera count, stream setup, or backend; regenerate
and revalidate it instead.

Run the matching workload with the generated profile:

~~~sh
sudo -v

.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config MATCHING_CONFIG.json \
  --case MATCHING_CASE \
  --policies deadline \
  --deadline-profile tools/realsense_steady_bench/profiles/WORKLOAD.csv \
  --nb-runs 1 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/deadline_smoke
~~~

By default, the transition occurs after 10 percent of the configured warm-up
deliveries, capped at one second of frames. Override it with
--deadline-apply-after-frames; the value must remain below --warmup-frames.

The preload tracer identifies a worker by an ASLR-independent pthread creation
stack plus its live instance number. It intentionally excludes the initial
process main thread from profile generation and admission. Profile application
is strict: all live worker pthreads must match exactly, every row must satisfy
runtime <= deadline <= period, and every kernel admission request must succeed.
A mismatch or partial admission restores already changed threads to SCHED_OTHER
and aborts before measurement. Scheduler-configuration errors are not retried
as camera failures. The attempt stores a copy and SHA-256 digest of the exact
profile, and steady_summary.json records every TID and applied reservation.

Do not reuse a profile after rebuilding librealsense or changing the kernel,
stream configuration, number of cameras, or delivery mode. First collect new
SCHED_OTHER traces. For paper results, pool the planned independent calibration
traces rather than deriving a profile from a single trace.

When a profile was calibrated with `--v4l2-diagnostics`, validation normally
uses the same option so creation-stack signatures refer to the identical binary
layout. For a reservation-only validation where the large per-event V4L2 trace
is unnecessary, use `--v4l2-diagnostics-build-only`. This compiles the same
markers but leaves their trace sink disabled, preserving profile identity while
avoiding the diagnostic storage and recording overhead.

## Per-thread rate-monotonic RR and FIFO modes

The `rr-rm` and `fifo-rm` policies reuse the same workload-specific temporal
model as SCHED_DEADLINE, but use only its measured `period_ns` values. The
process and every worker initially run under SCHED_OTHER. During camera warm-up,
the probe strictly matches every live worker by creation-stack signature and
instance number, then assigns fixed priorities according to rate-monotonic
ordering:

~~~text
shorter period  -> higher numeric POSIX real-time priority
equal period    -> equal priority
longer period   -> lower priority
~~~

The shortest-period group receives the campaign RT priority (80 by default),
and each distinct longer period uses the next lower priority. The process main
thread remains SCHED_OTHER because it performs aperiodic phase coordination;
the application `rs-wait-N` acquisition threads are included in the generated
worker model. `rr-rm` uses SCHED_RR within each equal-priority group, whereas
`fifo-rm` uses SCHED_FIFO. Profile matching and policy application are
all-or-nothing: a mismatch or failed `sched_setscheduler()` call restores every
already changed worker to SCHED_OTHER and invalidates the run before
measurement.

For example:

~~~sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config MATCHING_CONFIG.json \
  --case MATCHING_CASE \
  --policies rr-rm fifo-rm \
  --scheduler-profile tools/realsense_steady_bench/profiles/WORKLOAD.csv \
  --nb-runs 3 \
  --serial CAMERA_A --serial CAMERA_B \
  --results-dir tools/realsense_steady_bench/results/rate_monotonic
~~~

The output distinguishes these policies as `SCHED_RR_RM` and
`SCHED_FIFO_RM`. Each row records the exact TID-to-period-to-priority mapping,
the number of priority levels, and the copied profile digest. The historical
`rr` and `fifo` modes remain available and continue to launch the entire
process at one flat priority; do not merge their results with the RM modes.

## Long Single-Camera Run

For a ten-minute, 30-frame/s acquisition:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --case one_camera_all_streams_wait \
  --policies other \
  --measurement-duration-seconds 600 \
  --nb-runs 5 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/one_camera_10min
```

## Five-Minute Parameter Exploration

`configs/parameter_exploration_5min.json` defines two single-camera workloads:

- representative: depth 848x480 Z16 at 30 frames/s and color 640x480
  RGB8 at 30 frames/s, with infrared disabled;
- stress: depth and both Y8 infrared streams at 848x480, plus color
  960x540 RGB8, all at 60 frames/s.

Each run excludes a ten-second warm-up and measures five minutes. The following
Benchkit Cartesian product runs both workloads under `SCHED_OTHER` and
`SCHED_RR`, with three repetitions per combination:

```sh
sudo -v

.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/parameter_exploration_5min
```

This creates twelve runs:

```text
2 workloads x 2 scheduling policies x 3 repetitions
```

The workload description and exact profiles are copied into `case.json` and
flattened into the Benchkit result CSV.

## Register-Only CPU Busy-Loop Noise

The `busy_loop` condition launches a `SCHED_OTHER` process containing a
configurable number of worker threads. Each worker repeatedly applies dependent
integer operations to a thread-private register value. The hot loop performs no
allocation, array traversal, `memcpy`, file access, system call, or shared-memory
update. It checks one signal flag per 4096 operations and writes its counters
only after termination. This makes the workload primarily compete for CPU
execution time and scheduler service while minimizing cache, memory-bandwidth,
I/O, and USB/DMA effects.

Set the worker count once per campaign with `--cpu-noise-workers N`. Increasing
the count occupies more logical CPUs until the selected affinity mask is
saturated. Counts above the number of available CPUs increase runnable-task
contention but cannot increase physical CPU execution throughput. Use
`--cpu-noise-cpu-affinity` to co-locate the workers with the camera and
librealsense threads.

A Raspberry Pi 5 dose-response pilot can run separate campaigns with 1, 2, 3,
and 4 workers. The formal matrix should then use one selected count rather than
turning worker count into another Cartesian factor. For example, the paired
four-worker experiment is:

```sh
sudo -v
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --cpu-noise-modes none busy_loop \
  --cpu-noise-workers 4 \
  --gpu-noise-modes none \
  --usb-storage-noise-modes none \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/cpu_busy_4workers_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 CPU-noise modes x 3 repetitions
```

The workload starts after all cameras have warmed and then warms for ten seconds
before steady-state measurement. Each record reports
aggregate process CPU time, CPU equivalents (`process CPU time / wall time`),
normalized worker utilization, completed register-loop iterations, start/stop
timestamps, worker count, and affinity. Optional timing controls are
`--cpu-noise-warmup-seconds` and `--cpu-noise-ready-timeout-seconds`.

## Fixed-Size Memory-Copy Noise

The `fixed_copy` condition isolates sustained host-memory contention from the
register-only CPU condition. Every `SCHED_OTHER` worker owns two aligned,
pre-touched buffers and alternates their roles as source and destination for
each `memcpy`. The default 64 MiB buffer is deliberately larger than ordinary
CPU caches. With `N` workers, the process allocates `2 x N x 64 MiB` by default.

Set a fixed dose per campaign with `--memory-noise-workers N` and
`--memory-noise-buffer-size-mib M`. More workers can increase memory-controller
pressure until bandwidth saturates, but they also consume CPU execution time;
the reported process CPU equivalents quantify that unavoidable component. Use
`--memory-noise-cpu-affinity` when a controlled CPU placement is required.
Use `--memory-noise-target-mib-per-second R` to pace the aggregate estimated
read-plus-write traffic across all workers. A value of `0` keeps the original
unlimited behavior. Ready and summary artifacts record the requested rate,
achieved rate, and achieved/target ratio. This rate is an estimate based on one
read and one write per copied byte, not a hardware memory-controller counter.
The default `--memory-noise-copy-chunk-kib 1024` rate-limits 1 MiB pieces while
still walking the full per-worker buffer. This avoids turning a low average
target into short full-bandwidth 64 MiB bursts. The chunk size must divide the
buffer size exactly; keep it fixed when comparing bandwidth levels.

For example, compare baseline and four-worker memory contention:

```sh
sudo -v
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --cpu-noise-modes none \
  --memory-noise-modes none fixed_copy \
  --memory-noise-workers 4 \
  --memory-noise-buffer-size-mib 64 \
  --memory-noise-copy-chunk-kib 1024 \
  --memory-noise-target-mib-per-second 2000 \
  --gpu-noise-modes none \
  --usb-storage-noise-modes none \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/memory_copy_4workers_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 memory-noise modes x 3 repetitions
```

After all cameras have warmed, the runner waits for ten seconds of copy progress
before opening the steady-state measurement gate. Each
record contains payload-copy MiB/s, an estimated read-plus-write memory traffic
rate, allocated bytes, buffer size, worker count, CPU equivalents, affinity,
and `CLOCK_BOOTTIME` boundaries. The read-plus-write rate is an algorithmic
estimate of two transferred bytes per copied payload byte, not a hardware
memory-controller counter.

## Read-Only USB Storage Noise

The `sequential_read` mode continuously reads an unmounted whole USB disk using
1 MiB `O_DIRECT` requests and wraps at the end of the device. It is deliberately
read-only: camera traffic and storage reads both flow device-to-host, while
writes would add flash wear and garbage-collection variability.

The runner performs safety checks before starting the campaign. The target must
be a whole USB block device, no partition on it may be mounted, and a stable
`/dev/disk/by-id/...` path is strongly recommended. After opening the device
read-only with elevated permission, the workload drops back to the invoking
UID/GID. It runs as `SCHED_OTHER`, reads for ten seconds before signalling ready,
and records achieved throughput and read-latency statistics for every run.

Run a paired five-minute read-noise comparison with GPU noise disabled:

```sh
sudo -v
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --gpu-noise-modes none \
  --usb-storage-noise-modes none sequential_read \
  --usb-storage-device /dev/disk/by-id/usb-MODEL_SERIAL-0:0 \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/usb_read_noise_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 USB-noise modes x 3 repetitions
```

Use `lsusb -t` and the recorded sysfs paths to verify topology. For shared-bus
experiments, use a SuperSpeed storage device on the same powered USB 3 hub as
the camera. A USB 2 device or a device on another xHCI controller does not
create direct SuperSpeed-link contention and must be labelled accordingly.
Optional controls are `--usb-storage-warmup-seconds`,
`--usb-storage-block-size-kib`, and `--usb-storage-ready-timeout-seconds`.

## Selected GPU-Noise Workload

The formal GPU-interference condition is `mobilenet_v2_vulkan`. It continuously
executes the pinned ncnn MobileNetV2 224x224x3 computation graph on a hardware
Vulkan device. `none` is the paired baseline. Synthetic Vulkan arithmetic and
GPU memory-copy loops, ResNet18, and YOLO are intentionally not exposed as formal
benchmark modes.

The graph uses deterministic zero weights and a fixed input tensor. Classification
accuracy is irrelevant to this interference workload; this choice makes every
run independent of external model downloads while preserving the exact
MobileNetV2 layer graph and GPU command stream used during candidate selection.
The ncnn git revision and SHA-256 of `mobilenet_v2.param` identify the workload.

After every camera has warmed, the runner starts one `SCHED_OTHER` noise process
and waits for model loading plus ten warm-up inferences. Only after
`gpu_noise_ready.json` exists does steady-state measurement begin. At the end of the run,
the process handles `SIGTERM`, completes its in-flight inference, and writes
latency and iteration statistics. A software Vulkan CPU device is rejected, so
llvmpipe cannot silently turn this into CPU noise. On Raspberry Pi, the runner
auto-selects `/usr/share/vulkan/icd.d/broadcom_icd.json` when it exists.

Run a paired five-minute comparison for both workloads and two policies:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --config tools/realsense_steady_bench/configs/parameter_exploration_5min.json \
  --policies other rr \
  --gpu-noise-modes none mobilenet_v2_vulkan \
  --nb-runs 3 \
  --serial CAMERA_SERIAL \
  --results-dir tools/realsense_steady_bench/results/gpu_noise_5min
```

This creates 24 runs:

```text
2 workloads x 2 policies x 2 GPU-noise modes x 3 repetitions
```

Optional controls include `--gpu-noise-device`,
`--gpu-noise-warmup-iterations`, `--gpu-noise-ready-timeout-seconds`,
`--gpu-noise-vulkan-icd`, and `--gpu-noise-cpu-affinity`. CPU affinity is left
unset by default to match the candidate-selection experiment.

## Multiple Cameras

Pass one serial per camera. Each selected camera gets its own pipeline:

```sh
.venv/bin/python tools/realsense_steady_bench/run_steady_campaign.py \
  --case one_camera_all_streams_wait \
  --policies other \
  --frames 18000 \
  --nb-runs 5 \
  --serial SERIAL_1 \
  --serial SERIAL_2 \
  --results-dir tools/realsense_steady_bench/results/two_cameras
```

Command-line serials override the case configuration and set `camera_count`
accordingly. Keep physical hub/controller labels in a copied JSON configuration
so that the result CSV identifies the actual setup.

## Retained RSUSB support

RSUSB build and UVC unbind/rebind support remains in the codebase for separate
backend validation. It is not used by the paper campaign, which fixes
`CAMPAIGN_BACKEND = "v4l2"` and uses the kernel `uvcvideo` driver. Any RSUSB
validation must change the explicit constants in the runner and use a separate
build and results directory; backend results must never be mixed in one
campaign.

## Result Layout

Every Benchkit record directory uses the following indexed layout:

```text
case.json
run_manifest.json
attempts.json
selected_attempt.txt
steady_summary.json
probe_stdout.txt
cpu_frequency_lock.txt
attempt-1/
  attempt_manifest.json
  memory_cleanup_before_run.json
  topology_before.json
  topology_after.json
  steady_summary.json
  kernel_log.txt
  frame_events.csv
  probe_stdout.txt
  *_noise_configuration.json
  *_noise_ready.json
  *_noise_summary.json
  *_noise_process.json
  thread_lifecycle.jsonl
  lime_trace/
  thread_steady_intervals.csv
  thread_steady_activations.csv
  thread_steady_summary.csv
  thread_steady_summary.json
attempt-2/
  ...
```
`frame_events.csv` is the source for frame-loss and inter-arrival analysis.
`thread_steady_activations.csv` groups scheduler fragments into jobs separated
by blocking/sleep intervals; preemption does not incorrectly create a new job.
The summary uses complete jobs when possible and reports partial jobs at the
measurement boundaries separately in the raw activation CSV.

## Paper Metrics

Primary response variables should include:

- frame-number loss rate per camera and stream;
- host inter-arrival p99, p99.9, and maximum;
- sensor versus host inter-arrival divergence;
- acquisition-thread execution, ready, response, and period tails;
- UVC/xHCI interrupt deltas, UVC resubmit errors, and CPU placement.

Startup time is deliberately not mixed into these values: the measurement
window starts only after all cameras have warmed up.
