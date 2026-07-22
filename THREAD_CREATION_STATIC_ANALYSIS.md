# THREAD_CREATION_STATIC_ANALYSIS

## Workspace Baseline

- Repository root: `.`
- librealsense source root: `deps/librealsense`
- librealsense commit: `9d7cc8ed9b4080a4e00fc68eb57e3d8961c6938a` (`Push 2.58.2 to master (#15206)`)
- Build system: top-level CMake project, vendored `add_subdirectory(deps/librealsense EXCLUDE_FROM_ALL)`
- Trace probe: `src/realsense_thread_lifecycle_probe.cpp`
- Debug build script: `tools/realsense_thread_trace/build_librs_debug.sh`
- Verified runtime library path from `tools/realsense_thread_trace/output/ldd.txt`:
  `build-realsense-thread-trace/RelWithDebInfo/librealsense2.so.2.58`
- Runtime trace used for evidence: `tools/realsense_thread_trace/output/thread_trace.jsonl`
- Symbolized trace used for evidence: `tools/realsense_thread_trace/output/symbolized_thread_trace.json`
- Camera observed in trace: Intel RealSense D435, serial `327122075717`, firmware `5.17.0.10`,
  physical port `/sys/devices/pci0000:00/0000:00:14.0/usb2/2-7/2-7:1.0/video4linux/video2`

## Static Thread-Creation Table

| ID | Creation wrapper/class | Creation function | Thread entry function | File:line | Likely role | Evidence |
| -- | ---------------------- | ----------------- | --------------------- | --------- | ----------- | -------- |
| T01 | `dispatcher` | `dispatcher::dispatcher(unsigned int, ...)` | constructor lambda taking items from `_queue` | `deps/librealsense/third-party/rsutils/src/dispatcher.cpp:14` | queue-backed asynchronous dispatcher | Source comment says it keeps a running thread that takes items off the queue and dispatches them. Destructor joins the worker. Runtime stacks repeatedly resolve creator and inferred entry to `dispatcher.cpp:14`. Confidence: high. |
| T02 | `active_object<T>` | `active_object` owns a `dispatcher(1)` and calls `do_loop()` | dispatcher-invoked `_operation(ct)` loop | `deps/librealsense/third-party/rsutils/include/rsutils/concurrency/concurrency.h:407`, `:443` | repeating cancellable operation built on `dispatcher` | Source shows `active_object` contains a `dispatcher`, `start()` invokes `do_loop()`, and `do_loop()` reinvokes itself until `_stopped`. Runtime stacks show callers such as `udev_device_watcher` and `time_diff_keeper` entering `dispatcher.cpp:14`. Confidence: high for wrapper, role depends on owner class. |
| T03 | `udev_device_watcher` via `active_object` | `udev_device_watcher::udev_device_watcher` | lambda polling `_udev_monitor_fd` | `deps/librealsense/src/linux/udev-device-watcher.cpp:95` | device hotplug watcher/poller | Source lambda polls udev monitor fd with `POLLING_PERIOD_MS = 100`. Runtime creation stack includes `udev_device_watcher.cpp:95` through `dispatcher.cpp:14` during `rs2::context` construction. Confidence: high. |
| T04 | libusb internal event thread | `libusb_init` / `libusb_init_context`, called by `usb_context::usb_context()` | libusb internal routine, named `libusb_event` at runtime | librealsense call site `deps/librealsense/src/libusb/context-libusb.cpp:21`; dependency frame in `/lib/x86_64-linux-gnu/libusb-1.0.so.0` | libusb event handling during USB enumeration/context setup | Runtime trace has short-lived threads named `libusb_event`; creation stacks show `libusb_init_context` called from `librealsense::platform::usb_context::usb_context()`. Entry source is unavailable because it is inside system libusb. Confidence: high for origin, medium for internal entry details. |
| T05 | `usb_context` explicit event handler | `usb_context::start_event_handler()` | lambda loop calling `libusb_handle_events_completed(_ctx, ...)` | `deps/librealsense/src/libusb/context-libusb.cpp:92` | libusb event handling when requested by users of `usb_context` | Source explicitly assigns `_event_handler = std::thread([this](){ while (!_kill_handler_thread) libusb_handle_events_completed(...) });`. This path was not clearly distinguished from libusb internal `libusb_event` in the current D435 run. Confidence: medium, static-only in this run. |
| T06 | V4L2 UVC backend | `v4l_uvc_device::stream_on(...)` | lambda calling `capture_loop()` | `deps/librealsense/src/linux/backend-v4l2.cpp:1665` | video/depth frame acquisition from V4L2 | Source starts `_thread = new std::thread([this](){ capture_loop(); })` after `streamon()`. Runtime trace has two long-lived threads created by this function during `pipeline.start()`, both exiting at `pipeline.stop()`. Confidence: high. |
| T07 | `time_diff_keeper` via `active_object` | `time_diff_keeper::time_diff_keeper(...)` and `time_diff_keeper::start()` | lambda calling `polling(cancellable_timer)` | `deps/librealsense/src/global_timestamp_reader.cpp:177`, `:185` | periodic hardware/system timestamp correlation polling | Source lambda calls `polling`; `polling()` calls `update_diff_time()` and sleeps based on `_poll_intervals_ms`. Runtime stacks include `global_timestamp_reader.cpp:177` via dispatcher creation during device construction. Confidence: high. |
| T08 | `notifications_processor` | `notifications_processor::notifications_processor()` owns `_dispatcher(10)`; `set_callback()` starts it | dispatcher queue worker | `deps/librealsense/src/core/notification.cpp:26`, `:38` | asynchronous notification callback dispatch | Source owns a dispatcher, starts it when callback is set, and stops it in destructor. Runtime creation stacks include `notifications_processor::notifications_processor()` and `sensor_base` construction paths. Confidence: high. |
| T09 | UVC streamer publish active object | `uvc_streamer` setup of `_publish_frame_thread` | lambda dequeuing backend frames and calling user callback | `deps/librealsense/src/uvc/uvc-streamer.cpp:88` | frame publication from backend queue | Source creates `std::make_shared<active_object<>>` whose operation dequeues `_queue` and calls `_context.user_cb`. Not clearly observed in this V4L2 D435 run. Confidence: medium, static-only in this run. |
| T10 | `watchdog` via `active_object` | `watchdog::watchdog(...)` | lambda sleeping timeout and invoking `_operation()` | `deps/librealsense/third-party/rsutils/include/rsutils/concurrency/concurrency.h:458` | periodic watchdog action | Source creates `_watcher = std::make_shared<active_object<>>` and `uvc_streamer.cpp` starts `_watchdog`. Not clearly observed in this run. Confidence: medium, static-only in this run. |
| T11 | HID device interrupt handling | `hid_device::start_capture(...)` | lambda calling `handle_interrupt()` | `deps/librealsense/src/hid/hid-device.cpp:150` | HID interrupt/event handling | Source creates `_handle_interrupts_thread = std::make_shared<active_object<>>([this] ... handle_interrupt())`. Not triggered by the depth+color-only D435 trace. Confidence: medium, static-only for this run. |
| T12 | Linux HID backend custom sensor | `hid_custom_sensor::start_capture(...)` | lambda reading HID fd/select loop | `deps/librealsense/src/linux/backend-hid.cpp:258` | HID custom sensor data acquisition | Source creates `_hid_thread` and loops on fd sets. Not triggered by this run. Confidence: medium, static-only. |
| T13 | Linux HID backend IIO sensor | `v4l_hid_sensor::start_capture(...)` | lambda reading IIO/HID data | `deps/librealsense/src/linux/backend-hid.cpp:539` | HID/IIO data acquisition | Source creates `_hid_thread` and loops over raw data. Not triggered by this run. Confidence: medium, static-only. |
| T14 | Linux HID async power-management workaround | backend HID initialization | lambda writing trigger sysfs attribute with retry | `deps/librealsense/src/linux/backend-hid.cpp:821` | delayed HID/IIO power-management setup | Source comment says async initialization may fail to map IIO triggers and the patch rectifies behavior; thread retries `write_fs_attribute`. Not triggered by this run. Confidence: medium, static-only. |
| T15 | Platform camera initialization | `platform_camera` constructor/init path | lambda owned by `_init_thread` | `deps/librealsense/src/platform-camera.cpp:164` | platform camera initialization | Static search finds `_init_thread = std::thread([this](){ ... })`. Not observed in current D435/V4L2 run. Confidence: low for this run. |
| T16 | Auto-exposure algorithm | `auto_exposure_state` path | thread owned by `_exposure_thread` | `deps/librealsense/src/algo.cpp:57` | auto-exposure background work | Static search finds `_exposure_thread = std::make_shared<std::thread>(...)`. Not observed in current run. Confidence: low for this run. |
| T17 | Record/playback dispatchers | `record_device`, `playback_device`, `playback_sensor` | dispatcher worker(s) | `deps/librealsense/src/media/record/record_device.cpp:16`; `deps/librealsense/src/media/playback/playback_device.cpp:40`; `deps/librealsense/src/media/playback/playback_sensor.cpp:99` | rosbag record/playback asynchronous IO/frame dispatch | Source owns lazy/shared dispatchers for read/write and per-stream dispatch. `BUILD_ROSBAG2=OFF` and current live-camera run did not trigger these. Confidence: medium for record/playback builds, static-only here. |

## Runtime-Observed Creation Classes In The Current Trace

- `dispatcher` workers: many short-lived enumeration/device-construction workers and several long-lived workers that exit near object destruction or process exit.
- `libusb_event`: short-lived threads around USB enumeration/context setup; entry is inside system libusb, but librealsense call path is symbolized to `context-libusb.cpp`.
- `v4l_uvc_device::stream_on` capture threads: two long-lived acquisition threads created during `pipeline.start()` and joined during `pipeline.stop()`.

## Notes And Limits

- This document is a preliminary static analysis grounded in local source plus one runtime trace.
- `Likely role` is marked high only when supported by source text/class names and/or the symbolized runtime stack.
- Static-only entries are not claimed to occur in the current minimal D435 depth+color trace.
- Kernel UVC worker threads, IRQ threads, camera firmware threads, and external processes are outside the pthread tracer scope.
