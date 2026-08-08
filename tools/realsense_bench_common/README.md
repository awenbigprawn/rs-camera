# Shared RealSense benchmark infrastructure

This package contains only behavior shared by the startup and steady-state
Benchkit campaigns. Probe arguments, trace interpretation, workload-specific
success criteria, noise workloads, and scientific metrics remain in their
benchmark directories.

| Module | Responsibility |
| --- | --- |
| `settings.py` | Paper-wide backend, USB driver, CPU frequency, RT priority, recovery defaults, policy names, and repository paths |
| `commands.py` | `chrt`, LiME, `LD_PRELOAD`, sudo, and pthread tracer command construction |
| `attempts.py` | Generic logical-run attempt loop, retry decisions, recovery aggregation, and common metadata |
| `recovery.py` | Firmware plus parent composite-USB reset for one or more selected cameras |
| `system_controls.py` | CPU lock/exact restore, RealSense autosuspend enforcement, optional RSUSB UVC binding, topology snapshots, and kernel-log windows |
| `cpu_isolation.py` | Reversible cgroup-v2 CPU partitioning, automatic RealSense-to-xHCI IRQ discovery, IRQ affinity, and exact campaign cleanup |
| `realsense_devices.py` | Shared D415/D435/D455 USB discovery, model identification, and upstream-hub metadata |
| `memory.py` | Filesystem cache dropping and its result fields |
| `artifacts.py` | Selected-attempt resolution for canonical and historical layouts |
| `results.py` | Common retry and recovery CSV fields |

The benchmark adapter classifies each attempt. Startup classifies every failed
probe as a startup failure and may retry it. Steady state performs a full reset
and retries camera failures both before and after the global
`steady_state_begin` marker. Scheduler and noise-setup errors are configuration
failures, so they are neither hidden by camera resets nor retried.

All camera-acquisition entry points use two reset layers by default. Before
attempt 1, every camera selected by the logical run receives a firmware reset
and a reset of its parent composite USB device. This establishes an independent
device baseline instead of inheriting pipeline state from the preceding run.
If startup or warm-up still fails, the failed attempt is preserved, every
selected camera is reset again, and only that same logical run is retried, up
to three attempts. `--no-reset-before-run` and `--recover-on-failure none` are
diagnostic opt-outs, not formal experiment settings.

The canonical result layout retains every attempt:

```text
run/
  run_manifest.json
  attempts.json
  selected_attempt.txt
  attempt-1/
  attempt-2/
```

`resolve_selected_attempt()` also supports older steady campaigns where the
selected attempt's contents were promoted directly into `run/`.
