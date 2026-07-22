# RealSense USB Topology Benchmark

This directory is for the first RTNS experiment group: separating USB/I/O topology effects from CPU scheduler effects.

It uses `deps/benchkit` to run a reproducible campaign and a local C++ probe to measure RealSense frame timing.

## What It Measures

The C++ probe records:

- `wait_for_frames` blocking time, for `--delivery wait`
- host-side frameset inter-arrival time
- callback inter-arrival time, for `--delivery callback`
- per-stream host inter-arrival time
- per-stream sensor timestamp inter-arrival time
- frame-number drops per stream
- active RealSense streams and USB descriptor reported by librealsense

The campaign also saves before/after topology snapshots for every run:

- `lspci`
- `lsusb -t`
- `lsusb`
- `/proc/interrupts`
- parsed xHCI/UVC interrupt counters

Physical variables such as USB2 vs USB3, hub/no-hub, same root hub vs different controller, and number of cameras are not changed by software. They are labels attached to each case and must be produced by physically changing the setup. The snapshots are the evidence for the actual condition.

## Files

- `src/realsense_usb_latency_probe.cpp`: measurement binary linked against librealsense.
- `run_usb_topology_campaign.py`: benchkit campaign runner.
- `snapshot_topology.py`: captures USB/xHCI topology and interrupt counters.
- `configs/smoke_matrix.json`: short one-camera smoke matrix.
- `configs/full_matrix_template.json`: template for the full RTNS matrix.

## Build

From the repo root:

```bash
cmake -S . -B build-realsense-thread-trace -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build-realsense-thread-trace --target realsense_usb_latency_probe -j4
```

## Run A Smoke Campaign

```bash
PYTHONPATH=deps/benchkit \
python3 tools/realsense_usb_topology_bench/run_usb_topology_campaign.py \
  --config tools/realsense_usb_topology_bench/configs/smoke_matrix.json \
  --nb-runs 1 \
  --duration-sec 10
```

Results are written under:

```text
tools/realsense_usb_topology_bench/results/
```

Each benchkit record directory contains:

```text
case.json
topology_before.json
topology_after.json
probe_result.json
probe_stdout.json
```

## Run One Case

```bash
PYTHONPATH=deps/benchkit \
python3 tools/realsense_usb_topology_bench/run_usb_topology_campaign.py \
  --case current_1cam_depth_color_30_wait \
  --nb-runs 3 \
  --duration-sec 60
```

## Run Probe Directly

```bash
build-realsense-thread-trace/realsense_usb_latency_probe \
  --stream-mode depth_color \
  --delivery wait \
  --width 640 --height 480 --fps 30 \
  --duration-sec 10 \
  --output tools/realsense_usb_topology_bench/results/manual_probe.json
```

Full D435 hardware streams:

```bash
build-realsense-thread-trace/realsense_usb_latency_probe \
  --stream-mode d435_all \
  --delivery callback \
  --width 640 --height 480 --fps 30 \
  --duration-sec 10
```

## Suggested Physical Campaign Labels

Use one result directory per physical setup:

```text
usb2_direct_1cam
usb3_direct_1cam
usb3_hub_1cam
usb3_same_xhci_2cam
usb3_separate_controller_2cam
usb3_same_xhci_4cam
```

Before a long run, verify the actual topology:

```bash
lsusb -t
lspci | grep -i xhci
```

A D435 that appears under a `480M` bus is in USB2 High-Speed mode. For the USB3 condition it should appear under the SuperSpeed bus, typically `5000M` or `10000M` depending on the host controller/root hub display.

## Metrics To Use In The Paper

Primary:

- `frameset_interarrival_ms_p99`, `p999`, `max`
- `wait_ms_p99`, `p999`, `max`
- `callback_gap_ms_p99`, `p999`, `max`
- per-stream `sensor_interarrival_ms_*`
- total `drops`

Secondary:

- `device_usb_type`
- `irq_delta_json`
- saved `lsusb -t` snapshots
- active stream list in `probe_result.json`

For long runs, use at least 10 minutes per case after a short warm-up, and repeat each case at least 5 times.
