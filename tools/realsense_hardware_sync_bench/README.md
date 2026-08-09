# RealSense D435 hardware-sync smoke benchmark

This small front end reuses the steady-state benchmark while configuring one
D435 depth sensor as the hardware-sync master and the other as a slave. It is
intended as an operational check before adding hardware synchronization to a
larger experiment matrix.

The probe writes `Inter Cam Sync Mode = 2` to the slave before writing mode 1
to the master. It starts the slave pipeline first, starts the master last,
warms up both cameras, measures depth frames, stops the master first, and
restores the original sync modes. Every write is verified by reading the
option back. Full camera reset and per-run retry come from the steady benchmark.

The initial smoke workload is depth-only, 848x480 Z16 at 30 FPS. On D435,
master/slave synchronization applies to depth; the RGB sensor is not physically
synchronized by these modes. Connect sync pin 5 between cameras and ground pin
9 between cameras before running the test.

```sh
cd ~/program/rs-camera
sudo -v
.venv/bin/python \
  tools/realsense_hardware_sync_bench/run_hardware_sync_benchmark.py \
  --master 327122075717 \
  --slave 948122073863 \
  --duration-seconds 30 \
  --results-dir tools/realsense_hardware_sync_bench/results
```

The session directory contains the generated steady case, all normal Benchkit
artifacts, and `hardware_sync_analysis.json`. The timing check reports the
variation of the inter-camera sensor-timestamp offset. It is a smoke check, not
a calibrated exposure-skew measurement; D400 camera clocks can have an offset
and drift, so electrical validation with a common observed event or an
oscilloscope is needed for a precise synchronization-error claim.

For a short stress-mode A/B comparison with kernel xHCI IRQ tracing, run the
same command twice and change only the sync mode:

```sh
# Asynchronous control
.venv/bin/python tools/realsense_hardware_sync_bench/run_hardware_sync_benchmark.py \
  --master 327122075717 --slave 948122073863 \
  --workload stress --fps 60 --duration-seconds 30 --warmup-seconds 10 \
  --no-hardware-sync --kernel-irq-trace

# Hardware-synchronized treatment
.venv/bin/python tools/realsense_hardware_sync_bench/run_hardware_sync_benchmark.py \
  --master 327122075717 --slave 948122073863 \
  --workload stress --fps 60 --duration-seconds 30 --warmup-seconds 10 \
  --hardware-sync --kernel-irq-trace
```

The companion `compare_usb_irq_pressure.py` reads equal-duration warm-up
windows from the saved traces and reports per-controller IRQ rates,
cross-controller nearest-IRQ deltas, and 100--1000 us burst-concentration
metrics. A failed strict warm-up remains labelled as failed, but its
pre-failure IRQ trace can still diagnose whether hardware synchronization made
the USB work more temporally concentrated without weakening depth freshness.

Reference: Intel RealSense, [Multiple Depth Cameras Configuration](https://dev.intelrealsense.com/docs/multiple-depth-cameras-configuration%C2%A0).
