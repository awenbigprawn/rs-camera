# rs-camera

A small C++ repo for testing librealsense real-time behavior.

## Build

By default this project builds against the vendored librealsense source in
`deps/librealsense`.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target smallest_test d435_sensor_probe -j 4
```

To use an installed librealsense package instead:

```bash
cmake -S . -B build -DRS_CAMERA_USE_SYSTEM_LIBREALSENSE=ON
cmake --build build --target d435_sensor_probe -j 4
```

## Programs

### `smallest_test`

Starts a default `rs2::pipeline`, waits for frames, and records timing around
`wait_for_frames()` for real-time scheduling experiments.

```bash
./build/smallest_test
```

### `d435_sensor_probe`

Investigates an Intel RealSense D435/D435i-style device. It enumerates the
device, sensors, advertised stream profiles, selected active profiles,
intrinsics, extrinsics, frame samples, and Linux thread deltas from
`/proc/self/task`.

Useful commands:

```bash
./build/d435_sensor_probe --list-only
./build/d435_sensor_probe --frames 300
./build/d435_sensor_probe --single-ir
./build/d435_sensor_probe --serial 327122075717
```

Options:

- `--list-only`: enumerate device/sensor/profile information without starting streams.
- `--frames N`: stop after `N` framesets; without this, run until Ctrl-C.
- `--single-ir`: request only one infrared stream instead of IR1 and IR2.
- `--enable-all`: use librealsense `config.enable_all_streams()` instead of explicit profile selection.
- `--serial SERIAL`: select a specific camera by serial number.

The probe prefers these D435 profiles when available:

- Depth: `Z16 848x480 @ 30`
- Color: `RGB8 640x480 @ 30`
- Infrared 1: `Y8 848x480 @ 30`
- Infrared 2: `Y8 848x480 @ 30`
- Gyro/accel: only if the device exposes motion profiles

## D435 Findings

Verified connected camera:

- Device: `Intel RealSense D435`
- Serial: `327122075717`
- Product line: `D400`
- Product ID: `0B07`
- Firmware: `5.17.0.10`
- Sensors: `Stereo Module`, `RGB Camera`
- Advertised streams:
  - `Stereo Module`: depth, infrared 1, infrared 2
  - `RGB Camera`: color

This is a plain D435, not a D435i. In the checked librealsense source,
plain D435 PID `0x0b07` maps to `Intel RealSense D435` and is not in the
D400 HID/IMU PID set. D435i PID `0x0b3a` is in that set. So this D435
does not expose gyro/accelerometer streams.

Short verified capture:

```text
Depth      Z16 848x480 @ 30
Color      RGB8 640x480 @ 30
Infrared 1 Y8 848x480 @ 30
Infrared 2 Y8 848x480 @ 30
```

## Thread Notes

The current build configured librealsense with native Linux V4L2 backend
(`RS2_USE_V4L2_BACKEND`, `FORCE_RSUSB_BACKEND=OFF`).

Source-backed behavior relevant to this probe:

- `rs2::pipeline` owns one dispatcher thread for pipeline control/restart callbacks.
- `dispatcher` creates one `std::thread` and runs queued actions serially.
- The pipeline `syncer` and `aggregator` processing blocks run inline on backend callback threads; they do not create separate worker threads for this probe.
- Native Linux UVC/V4L2 creates capture threads for active UVC devices. For D435 this normally means the stereo module and RGB camera paths.
- RSUSB/libusb builds have a different shape: libusb event handling, dispatchers, and per-stream active-object publish/watchdog workers.
- HID motion sensors create HID read/power-management threads, but this only applies when a motion sensor is present.

Observed thread deltas from `./build/d435_sensor_probe --frames 5` on the
connected D435:

```text
after rs2::context:                 1 -> 2   delta +1
after device inventory:             2 -> 8   delta +6
after pipeline object construction: 8 -> 9   delta +1
after pipeline.start:               9 -> 18  delta +9
after pipeline.stop and destroy:    18 -> 14 delta -4
```
