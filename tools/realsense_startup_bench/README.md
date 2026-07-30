# RealSense startup timing benchmark

This Benchkit campaign characterizes the transient startup and shutdown threads
of librealsense on a D435. Each recorded run performs ten complete cycles:

1. construct a new rs2::context and rs2::pipeline;
2. request depth, color, infrared 1, and infrared 2, with no silent fallback;
3. start streaming and receive ten framesets;
4. stop and destroy all librealsense objects;
5. wait until every thread created after the process baseline has exited;
6. only then begin the next cycle.

The workload is src/d435_sensor_probe.cpp. It is scheduling-policy neutral. The
campaign applies SCHED_OTHER, SCHED_RR, or SCHED_FIFO externally with chrt, so
the main thread and normally-created child threads inherit the selected policy.

## Campaign factors and fixed controls

| Role | Setting |
| --- | --- |
| Cartesian factor | scheduling policy (`other`, `rr`, or `fifo`) |
| Operational inputs | camera serial, cycles, frames, repetitions, timeouts, and output path |
| Fixed controls | V4L2 backend, `uvcvideo`, 1500 MHz CPU, cache drop before each run, RT priority 80, and 0 ms cycle delay |

Fixed controls are declared near the top of `run_startup_campaign.py` and are
copied into the Benchkit CSV and per-run manifest. They are intentionally not
command-line factors.

## Why two traces?

LiME's existing eBPF tracer is authoritative for timing. Its scheduler events
measure when a thread is actually running, sleeping, or runnable but waiting.
The repository's LD_PRELOAD helper adds pthread_create, trampoline-start,
join/detach, name, and exit semantics. Both application-side sources use
CLOCK_BOOTTIME, the same clock domain as LiME's bpf_ktime_get_boot_ns().
No LiME source code is modified.

This division matters: elapsed thread lifetime is not execution time. The
exporter reports:

- running: summed sched_switched_in to sched_switched_out intervals;
- sleeping: a switch-out with task state greater than zero until wake-up;
- ready: wake-up or preemption until the next switch-in;
- create/start/exit/join: pthread lifecycle and process phase timestamps.

## Prerequisites

Initialize the submodules, prepare the Benchkit Python environment, and build the unmodified LiME dependency:

    git submodule update --init --recursive
    python3 -m venv .venv
    .venv/bin/pip install -e deps/benchkit numpy
    cargo build --release --manifest-path deps/lime-rtw/Cargo.toml

LiME requires a Linux kernel with BTF/eBPF support and root privileges. RR/FIFO
also require root or CAP_SYS_NICE. Connect the D435 over USB 3 and ensure no
other process owns it.

## CPU-frequency control

The campaign locks every cpufreq policy once, immediately before the first
measured run, and keeps that setting for the complete campaign. The paper
campaign frequency is fixed by `CAMPAIGN_CPU_FREQUENCY_MHZ = 1500` near the top
of `run_startup_campaign.py`. It invokes `scripts/lock_cpu_freq.sh` only once;
subsequent runs verify the lock without rewriting sysfs. After all runs, or
after an ordinary error or `Ctrl+C`, cleanup invokes
`scripts/restore_cpu_freq_default.sh` and then reapplies the exact
min/max/governor state captured before the lock. An uncatchable termination
such as `SIGKILL` still requires manual restoration.

CPU frequency is a fixed platform control, not a Cartesian variable or CLI
override. Each run records its before/after cpufreq state and temperature in
JSON and in the campaign CSV. Run `sudo -v` before starting so the one-time lock
and final restore can run non-interactively.

## Per-run filesystem-cache control

Before each logical Benchkit run, a shared pre-run hook executes `sync` and
writes `3` to `/proc/sys/vm/drop_caches`. This reclaims the Linux page cache,
dentries, and inodes before CPU-frequency verification, camera recovery,
tracing, and the measured process begin. It does not clear anonymous process
memory or swap.

The hook runs once per Benchkit repetition, not once per camera cycle or failed
recovery attempt. Each run records the operation, duration, and selected
`/proc/meminfo` values in `memory_cleanup_before_run.json`; the same status and
memory fields are copied to the campaign CSV. Run `sudo -v` before the campaign
so the privileged write can remain non-interactive. Cache cleanup is a fixed
paper-campaign control rather than an experimental variable.

## Run

From the repository root:

    .venv/bin/python tools/realsense_startup_bench/run_startup_campaign.py \
      --policies other rr fifo \
      --cycles 10 \
      --frames 10 \
      --nb-runs 3

The post-destruction quiescence delay is fixed by
`CAMPAIGN_CYCLE_DELAY_MS = 0` near the top of the runner. It is recorded as a
Benchkit constant and in every run manifest, but is not part of the policy
Cartesian product. Use `calibrate_startup_timings.py` when qualifying another
platform or firmware version. Change the fixed campaign constant only after a
separate calibration campaign; do not mix delay exploration with the paper's
scheduling-policy comparison.

For a persistent `Frame didn't arrive within N` state, use
`--recover-on-failure full-reset`. It first sends the D435 firmware hardware
reset and waits for disconnect/reconnect, then applies `usbreset` to the
re-enumerated parent USB device. The D435 is a composite USB device, so this
host reset covers its depth, RGB, and infrared UVC interfaces together.

With `--recover-on-failure depth-prime`, a failed workload and all of its traces
are written first. The runner then resolves `RS2_CAMERA_INFO_PHYSICAL_PORT` to
the selected camera's depth video node and performs a complete raw 848x480 Z16
V4L2 lifecycle: STREAMON, dequeue ten frames, and STREAMOFF. This recovery was
observed to restore librealsense after USB reset and USB reauthorization did not.
The recovery is not part of the measured workload and is intentionally opt-in
because it changes system state. `--recover-on-failure usb` remains available as
a separate host-side reset experiment.

Set `--max-attempts-per-run N` to retry the identical Benchkit point after
recovery. For example, with N=3, a frame timeout in `attempt-1` triggers the
selected recovery, the `--recovery-settle-seconds` delay, and then `attempt-2`
with the same policy, quiescence delay, cycle count, and repetition number. Every failed
attempt remains available for analysis. The default is one attempt, so recovery
alone prepares the camera for the next point but does not repeat the current
point.

When not already root, run `sudo -v` first. The runner uses sudo around LiME and
uses the cached credential non-interactively to capture each run's kernel-log
window. Use --no-sudo only if the necessary eBPF, dmesg, and scheduling
capabilities are already configured. Select
one camera with --serial SERIAL.

For a short functional campaign:

    .venv/bin/python tools/realsense_startup_bench/run_startup_campaign.py \
      --policies other --cycles 2 --frames 2 --nb-runs 1

The bounded workload and join timeout reduce the risk of leaving a high-priority
RT process running. Start with SCHED_OTHER and inspect the output before the
RR/FIFO campaign.

## Calibrate timeout and waiting parameters

Use the lightweight calibration tool before changing the benchmark defaults. It
does not run LiME, so its purpose is parameter selection rather than execution
time modelling. It still requests the same strict four-stream workload and ten
framesets per startup.

    sudo -v
    .venv/bin/python tools/realsense_startup_bench/calibrate_startup_timings.py \
      --serial 327122075717 \
      --build-dir build-realsense-thread-trace \
      --cycles-per-trial 10 \
      --trials 1 \
      --validation-trials 3 \
      --results-dir tools/realsense_startup_bench/results/timing_calibration_run1

The default preliminary calibration performs 220 complete pipeline startups:
190 across the settle/frame-timeout/join-timeout/cycle-delay candidates and 30
combined validation starts. Each candidate trial begins with a D435 firmware
reset followed by a reset of the composite USB parent, so a persistent failure
from one candidate cannot contaminate the next candidate.

The stages are evaluated in this order:

1. shortest successful wait after full camera recovery;
2. shortest frame timeout with 1.5x headroom over the measured
   `first_frame_wait_ms`;
3. shortest join timeout with 1.5x headroom plus the probe's 5 ms polling
   interval;
4. shortest successful post-cycle delay;
5. repeated validation of all selected values together.

For a stronger final calibration, use `--trials 3 --validation-trials 5`.
Candidate lists and the safety factor are configurable from the command line.
The tool never changes source defaults automatically.

The 2026-07-22 D435 calibration completed 22/22 trials and 220/220 startup
cycles. Across all stages, the maximum first-frame wait was 913.080 ms, so a
1.5x margin requires 1370 ms and selects the tested 1500 ms candidate. The
maximum thread-join wait was 2.796 ms; the same margin plus the 5 ms polling
interval selects 10 ms. Zero post-cycle delay and zero post-recovery settle time
both passed, while firmware reconnect and host enumeration maxima were
2620.547 ms and 129.244 ms. Accordingly, this machine's benchmark defaults are
1500 ms frame timeout, 10 ms join timeout, 0 ms cycle/recovery delay, 5000 ms
firmware-reset timeout, and 1.2 s enumeration timeout. These are calibrated
platform defaults, not universal D435 guarantees; repeat the stronger calibration
under the intended CPU/USB stress before paper measurements.

Every trial is preserved immediately under a timestamped result directory.
`candidate_results.csv` contains one row per trial, `cycle_results.csv`
contains one row per startup, `candidate_summary.csv` records qualification
decisions, and `recommendation.json` contains the provisional settings and
combined-validation result. Reset timing is also measured to recommend bounds
for firmware reconnect and USB re-enumeration.

## Outputs

Benchkit writes its campaign-level CSV below
tools/realsense_startup_bench/results/. Each logical Benchkit record directory
contains:

- `run_manifest.json`: requested policy, priority, CPU frequency, workload,
  recovery, and retry configuration;
- `cpu_frequency_before_run.json` and `cpu_frequency_after_run.json`: cpufreq
  policy, min/max/current frequency, governor, verification, and temperature;
- `cpu_frequency_lock.txt`: output from the one-time lock helper (first run only);
- `memory_cleanup_before_run.json`: per-run cache-drop status, duration, and
  before/after memory counters;
- `attempts.json`: ordered outcome and recovery metadata for every attempt;
- `selected_attempt.txt`: the attempt whose measurements populate the
  campaign row;
- `summary.json`: the selected attempt plus logical-run retry statistics;
- `probe_stdout.txt`: a convenience copy of the selected attempt output;
- `attempt-N/`: complete raw, reproducible artifacts for measured attempt N.

Each `attempt-N/` directory contains `run_manifest.json`, `kernel_log.txt`
(or `kernel_log_capture_error.txt`), `probe_stdout.txt`,
`thread_lifecycle.jsonl`, `lime_trace/`, `thread_timing.csv`,
`thread_intervals.csv`, `thread_events.csv`, `kernel_events.csv`, and
`summary.json`. A failed attempt also contains `recovery.json` when recovery
is enabled.

The campaign CSV includes `attempt_count`, `failed_attempt_count`,
`initial_attempt_success`, and `eventual_success`. Report the initial-attempt
failure rate separately: eventual success after recovery must not be treated as
a failure-free camera run. The CSV also includes `cycle_delay_ms`,
`uvc_resubmit_errors`, phase-specific
`uvc_resubmit_errors_startup`, `uvc_resubmit_errors_streaming`,
`uvc_resubmit_errors_teardown`, plus `uvc_resubmit_interfaces` and
`uvc_resubmit_error_codes`. `kernel_events.csv` maps every matching kernel event
to the preceding application phase. Teardown `-EPERM` events can be normal URB
cancellation and must not be counted as frame-delivery failures. A kernel log captured
successfully with no matching UVC errors is represented by zero errors; check
`kernel_log_captured` before interpreting that zero. Recovery adds
`recovery_attempted`, `recovery_count`, `recovery_method`,
`recovery_success`, and `recovery_error`.
Cache control adds `memory_cleanup_enabled`, `memory_cleanup_recorded`,
`memory_cleanup_success`, `memory_cleanup_duration_ms`, and before/after
`MemAvailable`, `Cached`, and `SReclaimable` fields.

## Build a startup-thread timing model

Use independent one-start processes and a longer streaming observation window
for the startup model. Sixty framesets provide about two seconds of steady
30-fps behavior without mixing later stop/restart cycles into the `t=0` model.
The calibrated timeout and recovery values are written explicitly here so the
measurement remains reproducible if defaults change:

    sudo -v
    .venv/bin/python tools/realsense_startup_bench/run_startup_campaign.py \
      --policies other \
      --recover-on-failure full-reset \
      --max-attempts-per-run 3 \
      --recovery-settle-seconds 0 \
      --recovery-wait-seconds 1.2 \
      --recovery-reset-timeout-ms 5000 \
      --frame-timeout-ms 1500 \
      --join-timeout-ms 10 \
      --cycles 1 \
      --frames 60 \
      --nb-runs 20 \
      --serial 327122075717 \
      --build-dir build-realsense-thread-trace \
      --results-dir tools/realsense_startup_bench/results/startup_model_other_20runs

RSUSB build, unbind, retry, and restore support remains in the repository for
separate backend validation. It is not used by the paper campaign:
`CAMPAIGN_BACKEND = "v4l2"` fixes librealsense to its V4L2 path with the kernel
`uvcvideo` driver. Changing the backend constants in the runner constitutes a
different experiment and must use a separate build and results directory. The
standalone `scripts/realsense_rsusb_uvc.sh` helper remains available for such
diagnostic work.

Then point the offline model builder at the generated timestamped benchmark
directory:

    .venv/bin/python tools/realsense_startup_bench/build_startup_model.py \
      --campaign-dir tools/realsense_startup_bench/results/startup_model_other_20runs/benchmark_HOST_realsense_startup_TIMESTAMP \
      --policy other \
      --cycle 1 \
      --output-dir tools/realsense_startup_bench/results/startup_model_other_20runs/model

The builder aligns the modal thread shape by creation order and creation phase,
identifies stable periodic work from LiME scheduler intervals, and preserves
initial failure statistics separately from successful retries. It writes raw
samples, a p05/p50/p95 per-thread table, JSON metadata, a Markdown report, and
an SVG horizontal running/ready/sleeping timeline.

Test the offline merger without a camera or root privileges:

    python3 -m unittest discover \
      -s tools/realsense_startup_bench/tests -v

## Interpretation cautions

A D435 has no IMU; a D435i adds motion streams and associated HID threads.
Thread counts also depend on librealsense's V4L2 versus RSUSB backend and kernel
configuration. Record the backend, firmware, USB topology, kernel, CPU affinity,
governor, and PREEMPT/PREEMPT_RT configuration with every paper campaign.
RR/FIFO inheritance must be verified from the per-thread policy column rather
than assumed.
