# rs-camera

C++ workloads and Python benchmark tooling for characterizing librealsense timing
on Intel RealSense cameras.

## Repository layout

- src/: every librealsense test executable, including the reusable D435 startup
  workload and the USB/thread probes.
- include/: shared C/C++ helpers such as application phase markers.
- tools/realsense_startup_bench/: Benchkit startup campaign and LiME/eBPF CSV
  merger.
- tools/realsense_thread_trace/: pthread lifecycle tracer and visualization
  tools.
- tools/realsense_usb_topology_bench/: USB topology/scaling campaign.
- deps/: pinned librealsense, LiME, and Benchkit dependencies.

## Build

On a fresh Ubuntu machine, install all system, Rust, Python, librealsense, and
LiME build dependencies from the repository root:

    ./scripts/install_dependencies_ubuntu.sh

Add `--build` to initialize the submodules, prepare `.venv`, and also compile
the native LiME binary, `d435_sensor_probe`, and the pthread tracer:

    ./scripts/install_dependencies_ubuntu.sh --build

On a Raspberry Pi kernel without the RealSense UVC format patches, use a
separate build directory and the RSUSB/libusb backend:

    ./scripts/install_dependencies_ubuntu.sh --build \
      --rsusb-backend \
      --build-dir build-realsense-rsusb

For a Benchkit RSUSB campaign, pass the camera's USB sysfs name so UVC is
unbound before every measured attempt (the name below is an example):

    --rsusb-usb-device 3-1

The campaign repeats the unbind because device recovery or re-enumeration can
reattach `uvcvideo`. After a successful campaign or an ordinary interruption,
it binds the interfaces back to `uvcvideo` so a later V4L2 run can use the
camera. The standalone helper remains available for non-campaign probes and
recovery after an uncatchable termination such as `SIGKILL`:

    sudo ./scripts/realsense_rsusb_uvc.sh unbind 3-1
    # Run one RSUSB probe.
    sudo ./scripts/realsense_rsusb_uvc.sh bind 3-1

Run the installer as a regular user. It invokes sudo only for apt and keeps the
Rust toolchain and project files owned by that user. See `--help` for
`--system-only`, `--skip-apt-update`, and build-directory overrides. On Ubuntu,
it also restores the standard `<codename>-updates` APT pocket when it is
missing, which prevents version conflicts between updated runtime libraries and
their development packages.

By default the project builds against the vendored librealsense source:

    cmake -S . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo
    cmake --build build --target \
      smallest_test \
      d435_sensor_probe \
      realsense_thread_lifecycle_probe \
      realsense_usb_latency_probe \
      -j4

To use an installed librealsense package:

    cmake -S . -B build \
      -DRS_CAMERA_USE_SYSTEM_LIBREALSENSE=ON \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo
    cmake --build build --target d435_sensor_probe -j4

## Programs

### smallest_test

Starts a default rs2::pipeline and records timing around wait_for_frames() for
small scheduling experiments.

### d435_sensor_probe

This is the reusable maximal-stream workload. On the connected D435 it selects:

- depth: Z16 848x480 at 30 fps;
- color: RGB8 640x480 at 30 fps;
- infrared 1: Y8 848x480 at 30 fps;
- infrared 2: Y8 848x480 at 30 fps.

A D435i also contributes its exposed motion profiles. The probe is deliberately
policy-neutral; apply SCHED_OTHER, SCHED_RR, or SCHED_FIFO externally with chrt.

Useful commands:

    ./build/d435_sensor_probe --list-only
    ./build/d435_sensor_probe --frames 300
    ./build/d435_sensor_probe \
      --cycles 10 --frames 10 --strict-streams --join-timeout-ms 10

For each cycle, it constructs a fresh context/pipeline, starts every selected
stream, receives the requested framesets, stops and destroys the objects, and
waits for all non-baseline threads to exit before another cycle begins. Structured
RS_SCHEDULER, RS_STARTUP_CYCLE, and RS_STARTUP_RESULT lines are printed for
automation.

Important options:

- --cycles N: repeat the complete create/start/stop/destroy lifecycle N times;
- --frames N: framesets per cycle; zero runs until interrupted;
- --strict-streams: fail if the dual-infrared request cannot start;
- --join-timeout-ms N: maximum time to wait for all cycle threads to exit;
- --single-ir: intentionally use only one infrared stream;
- --enable-all: use librealsense config.enable_all_streams();
- --serial SERIAL: select a camera.

### Other probes

- realsense_thread_lifecycle_probe: one longer phase-marked trace workload.
- realsense_usb_latency_probe: frame/drop/interarrival workload used by the USB
  topology benchmark.

## Startup timing campaign

See tools/realsense_startup_bench/README.md. The standard paper-oriented run is:

    python3 tools/realsense_startup_bench/run_startup_campaign.py \
      --policies other rr fifo \
      --priority 80 \
      --cycles 10 \
      --frames 10 \
      --nb-runs 3

Use the matching backend and build directory for a Raspberry Pi RSUSB run:

    python3 tools/realsense_startup_bench/run_startup_campaign.py \
      --rsusb-backend \
      --rsusb-usb-device 3-1 \
      --build-dir build-realsense-rsusb \
      --policies other \
      --cycles 2 \
      --frames 3 \
      --nb-runs 1

LiME/eBPF supplies the authoritative scheduler timestamps. The approved
LD_PRELOAD helper adds pthread create/start/join/exit labels. Python merges both
CLOCK_BOOTTIME timelines into per-thread timing, interval, and event CSV files.
No Perl is used by the benchmark, and no LiME source code is modified.

## Current D435 smoke result

The connected RealSense D435 (serial 327122075717, firmware 5.17.3.10, USB 3.2)
successfully completed two strict depth/color/dual-IR cycles. Each cycle showed
17 additional threads after startup and zero additional threads after object
destruction and the join gate.

The exact thread shape depends on the librealsense backend. Native V4L2 and
RSUSB/libusb builds must be treated as separate experimental configurations.
A plain D435 has no IMU; D435i motion/HID threads are a different workload.
