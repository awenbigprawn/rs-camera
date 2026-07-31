# Shared RealSense benchmark infrastructure

This package contains only behavior shared by the startup and steady-state
Benchkit campaigns. Probe arguments, trace interpretation, workload-specific
success criteria, noise workloads, and scientific metrics remain in their
benchmark directories.

| Module | Responsibility |
| --- | --- |
| `settings.py` | Paper-wide backend, USB driver, CPU frequency, RT priority, policy names, and repository paths |
| `commands.py` | `chrt`, LiME, `LD_PRELOAD`, sudo, and pthread tracer command construction |
| `attempts.py` | Generic logical-run attempt loop, retry decisions, recovery aggregation, and common metadata |
| `recovery.py` | Firmware plus parent composite-USB reset for one or more selected cameras |
| `system_controls.py` | CPU lock/exact restore, optional RSUSB UVC binding, topology snapshots, and kernel-log windows |
| `memory.py` | Filesystem cache dropping and its result fields |
| `artifacts.py` | Selected-attempt resolution for canonical and historical layouts |
| `results.py` | Common retry and recovery CSV fields |

The benchmark adapter classifies each attempt. Startup classifies every failed
probe as a startup failure and may retry it. Steady state retries only failures
before the global `steady_state_begin` marker; failures after that marker are
measured outcomes and are recovered without retry.

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
