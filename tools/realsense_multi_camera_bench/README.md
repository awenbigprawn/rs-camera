# Multi-camera RealSense steady benchmark

This directory is a topology-aware front end to
`tools/realsense_steady_bench`. It is intended for the available D400 set:
three D455 cameras, two D435 cameras, and one D415 camera. It deliberately
reuses the steady benchmark's fixed CPU frequency, cache cleanup, CPU/IRQ
isolation, all-camera warm-up barrier, freshness metrics, full-device reset,
logical-run retry, LiME tracing, and result schema.

The front end adds four controls that are essential with powered hubs:

1. a frozen serial/model/USB-port/hub/xHCI layout;
2. a preflight that rejects missing or extra cameras, USB 2 fallback, changed
   topology, and more than two cameras per 5 V / 3 A hub;
3. generated homogeneous and heterogeneous camera-count cases; and
4. one scheduler profile per exact model/count/topology case.

Do not connect all six cameras to the two 5 V / 3 A hubs. The conservative
limit for this setup is two cameras per hub and four active cameras total.
Changing the cameras or ports defines a new physical batch and requires a new
layout file.

## Camera identity registry

`known_cameras.json` is the persistent hardware identity registry. D400
cameras expose two different serial namespaces:

- `librealsense_serial` is the optical-module serial returned as
  `RS2_CAMERA_INFO_SERIAL_NUMBER`; campaign `--serial` arguments use it.
- `usb_descriptor_serial` is the ASIC serial exposed by the USB descriptor and
  Linux sysfs; topology, autosuspend, and physical USB discovery use it.

The `inventory` command joins these namespaces through the registry and writes
both into the frozen batch layout. It deliberately rejects an unregistered
camera instead of accidentally passing an ASIC serial to librealsense. Add
each newly observed camera to the registry only after querying both values.

## Workloads and suites

| Suite | Camera sets | Streams | Default duration/repetitions | Purpose |
|---|---|---|---|---|
| `smoke` | every connected camera separately | depth 848x480 Z16 + color 640x480 RGB8, 30 FPS | 60 s / 1 | Verify every serial, cable, profile, reset path, and freshness counter |
| `scaling` | nested, hub-balanced prefixes from 1 to 4 cameras | same common 30 FPS workload | 600 s / 3 | Camera-count and USB/topology scaling |
| `heterogeneous` | available model pairs, D455+D435+D415, and one mixed four-camera set | same common 30 FPS workload | 600 s / 3 | Separate composition from camera count |
| `stereo-smoke` | every D455/D435 separately, then homogeneous sets from 2 through the available count | depth + IR1 + IR2 at 848x480; D455 color 848x480, D435 color 960x540; all at 60 FPS | 60 s / 1 | Detect device-specific faults and validate simultaneous high-bandwidth profiles before the long run |
| `stereo-stress` | every D455/D435 separately, then homogeneous sets from 2 through the available count | depth + IR1 + IR2 at 848x480; D455 color 848x480, D435 color 960x540; all at 60 FPS | 600 s / 3 | High-bandwidth stereo-camera stress |
| `all` | union of the above without exact duplicate cases | both workloads | 600 s / 3 | Full batch after smoke passes |

The D415 is never placed in `stereo_all`: unlike the D435/D455 stereo models,
it is not assumed to provide two infrared streams. The common depth+color
profile is resolved and exercised by the smoke suite before any long run.
Stress profiles are explicitly model-specific. D435 uses 960x540 color at
60 FPS, while D455 uses its validated 848x480 color mode at 60 FPS; both use
848x480 depth and two infrared streams at 60 FPS. A D415 stress profile will
only be added after profile discovery and a physical smoke test on that model.

## Tomorrow: safe direct-run procedure

### 1. Wire and inventory one physical batch

Use at most two cameras on each powered hub. Prefer a balanced layout, with one
hub on each Raspberry Pi 5 blue USB 3 port. Before collecting the layout, close
RealSense Viewer and any other process using `/dev/video*`.

```sh
cd ~/program/rs-camera
git pull --ff-only
sudo -v

lsusb -t
.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  inventory \
  --output tools/realsense_multi_camera_bench/layout-batch1.json
```

Inspect the printed table. Every camera must report speed `5000` or higher.
The two serial columns must match the corresponding entry in
`known_cameras.json`.
The two `upstream_hub` values should correspond to the two external hubs. Keep
the generated layout with the data; it is part of the experiment definition.
The generated `scaling_order` starts with the most numerous model (D455 wins a
tie), keeps each count as a nested prefix, and spreads that model across hubs
where the wiring permits. It may be edited before the first run, but must then
remain unchanged with the frozen layout.

Suggested batches are:

- batch 1: D455-A, D455-B, D435-A, D415-A (two cameras per hub);
- batch 2: D455-C, D435-B plus two repeated reference cameras needed for the
  planned comparison;
- optional homogeneous-D455 batch: all three D455 cameras, distributed 2+1.

The exact allocation must be recorded by `inventory`; do not infer it from
camera labels.

### 2. Dry-run, then smoke

```sh
.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run \
  --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite smoke \
  --no-lime \
  --dry-run

.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run \
  --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite smoke \
  --no-lime
```

The campaign forces USB `power/control=on`. Before attempt 1 of every logical
run, it firmware-resets and composite-USB-resets every camera in that case. It
starts measurement only after every selected camera has completed warm-up with
advancing, complete frames. On a startup, warm-up, or measurement failure it
resets every camera again, then retries only that logical Benchkit run up to
three times. The wrapper gives heterogeneous devices 10 seconds for firmware
reset and 5 seconds for USB re-enumeration. These bounds apply to baseline and
failure-recovery resets, not to the measured steady interval.

Do not continue if smoke reports a profile-resolution failure, USB 2 speed,
timeouts after all retries, duplicate warm-up frames, or sequence gaps during
the warm-up health window.

Next, smoke every simultaneous combination before committing hours to it:

```sh
.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite scaling --duration-seconds 60 --nb-runs 1 --no-lime

.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite heterogeneous --duration-seconds 60 --nb-runs 1 --no-lime

.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite stereo-smoke --no-lime
```

### 3. Run the new SCHED_OTHER matrices

Start with LiME-enabled SCHED_OTHER data. These traces are also the calibration
source for modeled scheduling; old two-D435 profiles must not be reused for a
D455/D415 or different-count case.

```sh
.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run \
  --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite scaling \
  --policies other

.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run \
  --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite heterogeneous \
  --policies other

.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run \
  --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite stereo-stress \
  --policies other
```

Each non-smoke command defaults to three ten-minute repetitions. Results are
stored below a timestamped directory in
`tools/realsense_multi_camera_bench/results/`. Copy each completed timestamped
directory to the laptop before changing the physical batch.

### 4. Generate profiles and run modeled policies

Point the profile generator at one completed suite root. It requires at least
three successful SCHED_OTHER traces for every case and uses the cross-run
maximum execution demand, a 1.20 runtime margin, and a 0.91 period scale.

```sh
.venv/bin/python tools/realsense_multi_camera_bench/generate_profiles.py \
  --results-root tools/realsense_multi_camera_bench/results/MULTICAMERA_RESULT \
  --output-dir tools/realsense_multi_camera_bench/profiles/BATCH_AND_SUITE
```

Then use the same frozen layout and exact suite:

```sh
.venv/bin/python tools/realsense_multi_camera_bench/run_multicamera_campaign.py \
  run \
  --layout tools/realsense_multi_camera_bench/layout-batch1.json \
  --suite scaling \
  --policies rr-rm fifo-rm deadline \
  --profiles-dir tools/realsense_multi_camera_bench/profiles/BATCH_AND_SUITE
```

The profile filename is the generated `case_id` plus `.csv`. Admission failure
under SCHED_DEADLINE is a valid capacity result; do not silently substitute a
profile from another camera composition.

## Design constraints inherited from earlier experiments

- V4L2 + `uvcvideo` is the fixed backend. RSUSB remains supported elsewhere
  but is not used for paper data.
- CPU frequency is locked once per campaign to 1500 MHz and restored on exit.
- RealSense autosuspend is disabled and verified before attempts.
- Camera xHCI IRQs stay on the housekeeping CPU; benchmark and librealsense
  workers run in the isolated benchmark partition.
- Noise starts only after all selected cameras pass warm-up. The multi-camera
  discovery matrices use no noise; noise should be added only after a stable
  topology baseline exists.
- A fixed-duration run is evaluated against expected sensor frame numbers, not
  merely the number of `wait_for_frames()` returns. Duplicate, unique,
  sequence-gap, stale-frameset, timeout, and per-stream metrics remain enabled.
- The current Raspberry Pi kernels use the validated `UVC_URBS=16` setting.
  Keep kernel version/configuration identical across compared camera sets.
- Hub placement, xHCI controller, USB speed, camera firmware, and serials are
  experiment variables recorded in the generated layout and per-run artifacts.

## Estimated first-day budget

For one four-camera physical batch:

- smoke: four minutes of measurement plus setup/reset time;
- scaling: 1+2+3+4 cameras, three ten-minute runs = two hours;
- heterogeneous: normally four to five cases = two to 2.5 hours;
- stereo stress: depends on how many D435/D455 cameras are in the batch;
- modeled policies multiply the selected suite time by three policies and
  should be run only after the SCHED_OTHER profiles validate.

Run the smallest informative suites first and back up each suite before
rewiring. This prevents a late power, cable, or topology problem from
invalidating an entire day of measurements.
