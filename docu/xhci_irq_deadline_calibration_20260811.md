# xHCI IRQ `SCHED_DEADLINE` Calibration

## Decision

The threaded xHCI IRQ handlers can use `SCHED_DEADLINE`, but this is not a
good final policy for this workload. The handlers are sporadic and strongly
bursty. They use only about 3--8% CPU time each on average, but a safe 1 ms
CBS server for the two controllers requires:

```text
runtime  = 490 us
deadline = 1000 us
period   = 1000 us
```

The two reservations consume 98% of one CPU, or 24.5% of the four-core
Raspberry Pi 5 capacity. We therefore use `SCHED_FIFO` priority 90 for the
xHCI IRQ threads, including when the recurring `librealsense` workers use
`SCHED_DEADLINE`.

## Calibration

The CPU frequency was fixed at 1.5 GHz. Both experiments used V4L2,
`uvcvideo` with 16 UVC URBs, all four CPUs, four register-only CPU-noise
workers, and a 30 s measurement gate. The xHCI handlers ran as IRQ threads
using the temporary `threadirqs` boot option on the standard Linux 6.12
kernel.

| Workload | IRQ thread | Mean CPU use | Max activation | Max execution in 1 ms | 1.2x reservation |
|---|---:|---:|---:|---:|---:|
| Four cameras, representative 30 FPS | xHCI 1 | 3.59% | 226 us | 262 us | 320 us |
| Four cameras, representative 30 FPS | xHCI 2 | 3.68% | 218 us | 295 us | 360 us |
| Two D435 cameras, stress 60 FPS | xHCI 1 | 6.91% | 338 us | 404 us | 490 us |
| Two D435 cameras, stress 60 FPS | xHCI 2 | 7.08% | 364 us | 397 us | 480 us |

The final parameters use the cross-workload maximum and the same reservation
for both controllers. Linux admitted both IRQ threads with
`490/1000/1000 us`. A loss-free streaming scheduler trace measured maximum
1 ms demands of 324 and 401 us while that reservation was active. The more
demanding controller therefore retained about 22% budget headroom.

The functional validation without trace-to-disk interference completed the
60 FPS stress workload with no timeout, duplicate, sequence gap, stale
frameset, or UVC resubmit error. A separate validation that streamed every
scheduler switch to disk produced 12 duplicate frames; this run is retained
only for scheduler-budget analysis because the tracing I/O perturbed the
camera workload.

## Data

The compact backup is stored at:

```text
results/rpi5/xhci_deadline_calibration_20260811/
```

Important summaries:

- `sched_other_trace/xhci_deadline_calibration.json`
- `stress_sched_other_trace/xhci_deadline_calibration.json`
- `stress_deadline_validation_v2/xhci_deadline_validation_analysis.json`

The backup retains the binary `trace-cmd` files. Large text reports were
excluded because they can be regenerated with `trace-cmd report`.
