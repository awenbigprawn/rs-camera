# Three-D455 Same-Controller Resource-Model Validation (2026-08-18)

## 1. Question

Can the workload-level resource model predict the persistent worker count and
userspace CPU utilization of three identical D455 cameras from the existing
one- and two-D455 calibration traces?

This is a held-out same-model validation point.  It avoids the D435/D455 model
mixture in the earlier four-camera validation and places all cameras behind one
hub and one xHCI controller.

## 2. Reused Calibration

The calibration artifacts are stored under:

```text
results/rpi5/predictive_model_20260818/calibration/
```

The relevant cells use the representative profile:

- Depth: 848x480 Z16 at 30 FPS;
- Color: 640x480 RGB8 at 30 FPS;
- infrared: disabled;
- `SCHED_OTHER`, V4L2, UVC16, no acquisition affinity or CPU isolation;
- 1.5-GHz CPU frequency;
- 30 warm-up frames and a fixed 30-s measurement interval; and
- three LiME traces per cell.

The one-D455 median has 12 child threads and consumes 0.045254 CPU cores.  The
two-D455 median has 23 child threads and consumes 0.096369 CPU cores.

## 3. Prediction Fixed Before Measurement

For every worker family, the three-camera affine extrapolation is

```text
N3 = 2*N2 - N1.
```

The same expression is applied to the measured userspace running demand.  It
counts a process-wide family once and extends the camera-local increment seen
between one and two cameras.  The prediction was generated before inspecting
the three-camera results.

| Quantity | One D455 | Two D455 | Three-D455 prediction |
|---|---:|---:|---:|
| Child-thread count | 12 | 23 | 34 |
| Userspace CPU utilization | 0.045254 cores | 0.096369 cores | 0.147485 cores |
| Aggregate USB payload | 40.87 MiB/s | 81.74 MiB/s | 122.61 MiB/s |
| Analytical memory touch | 166.55 MiB/s | 333.11 MiB/s | 499.66 MiB/s |

USB payload and memory touch follow directly from the configured stream
profiles.  They are analytical workload demand, not independent hardware
counter measurements.  The held-out trace directly tests worker multiplicity,
userspace CPU demand, delivery timing, and freshness.

## 4. Held-Out Topology and Method

All three D455F cameras use the same powered 5-Gbit/s SuperSpeed hub on
`xhci-hcd.0`:

| Hub port | SDK serial | USB serial | Speed |
|---|---|---|---:|
| `3-1.1` | `311322302503` | `327743062558` | 5000 Mb/s |
| `3-1.2` | `311322304911` | `327743060882` | 5000 Mb/s |
| `3-1.3` | `311322304863` | `327743063467` | 5000 Mb/s |

The planned held-out cell uses the same representative profile and measurement
method as the calibration cells.  It requests three repetitions on
`6.12.96-rpi5-rt-btf-uvc16+`.  Autosuspend is disabled, each logical run starts
with full-reset recovery enabled, and LiME plus the pthread lifecycle
interposer reconstruct the persistent worker families.  A separate one-run
diagnostic disables both the pre-run reset and LiME so that a reset or tracing
side effect cannot be mistaken for a topology limit.

Reproduction command:

```sh
tools/realsense_steady_bench/run_d455_n3_resource_validation.sh
```

## 5. Results

The three-camera cell could not enter steady state, so it does not provide a
measured CPU-utilization validation point.  In the first campaign, four
consecutive startup attempts failed during V4L2 stream configuration.  The
kernel reported repeated UVC completion status `-71`, after which the upstream
hub at `3-1` disconnected and all three cameras re-enumerated.  The artifacts
were copied to:

```text
results/rpi5/d455_n3_resource_validation_20260818/
```

After changing the hub power supply, one diagnostic attempt was made without a
pre-run reset and without LiME.  The hub and all three cameras remained
enumerated at 5 Gbit/s, but stream setup still failed.  The only concurrent
kernel error was:

```text
uvcvideo 3-1.2:1.1: Failed to set UVC probe control : -32 (exp. 48).
```

This diagnostic is stored under:

```text
results/rpi5/d455_n3_power_retry_20260818/
```

Thus, the new supply removed the observed whole-hub reset but did not make the
three-camera stream configuration feasible.  No additional retries were made.

| Outcome | Prediction or observation |
|---|---:|
| Predicted child-thread count | 34 |
| Predicted userspace CPU utilization | 0.147485 cores |
| Predicted USB payload | 122.61 MiB/s |
| Predicted analytical memory touch | 499.66 MiB/s |
| Cameras completing warm-up | 0 of 3 |
| Steady-state measurement duration | 0 s |
| CPU prediction validated | No |

This result exposes a missing feasibility condition rather than a CPU-demand
prediction error.  The additive payload estimate is necessary for sizing the
workload, but it does not model UVC negotiation, endpoint reservation, hub
power integrity, hub-controller behavior, or simultaneous stream activation.
Those conditions must succeed before the steady-state CPU and memory model can
be instantiated.  The evidence is insufficient to distinguish endpoint or
controller limits from another hub-specific electrical or protocol problem.

## 6. Interpretation Boundary

- The CPU prediction is an empirical affine extrapolation from measured one-
  and two-camera traces.  It is not a WCET or schedulability guarantee.
- A successful 30-s cell validates short-window structure and resource demand;
  it cannot exclude rare long-run failures.
- This topology tests three cameras sharing one controller.  Its result must
  not be generalized to three cameras distributed over two controllers
  without recording the topology separately.
- The analytical memory-touch value is not a measured DRAM-bandwidth value.
- A failed stream-configuration gate cannot validate or invalidate the affine
  steady-state CPU prediction because the predicted recurring workload never
  starts.

## 7. Follow-Up with a Split Topology

The same three cameras later succeeded when distributed across both Raspberry
Pi 5 xHCI controllers in a 2+1 arrangement and started through the full-reset
gate.  That held-out experiment validates the 34-thread prediction exactly and
the userspace CPU prediction within 4.39%.  Its separate report is
`docu/d455_n3_split_resource_model_validation_20260818.md`.  This follow-up
does not invalidate the concentrated-topology failure; it shows that topology
feasibility must be checked before applying the steady-state resource model.
