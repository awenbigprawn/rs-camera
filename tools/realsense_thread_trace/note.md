I checked tools/realsense_thread_trace/ and the related RealSense code. The approach is:
run the RealSense probe under an LD_PRELOAD library that intercepts pthread APIs, writes
JSONL lifecycle events, then parse/symbolize those events into summaries and timelines.

Main Flow

1. run_trace.py builds the preload library from tools/realsense_thread_trace/
    trace_pthreads.c:1.
    See tools/realsense_thread_trace/run_trace.py:20.

2. It launches the target executable with:
    - LD_PRELOAD=/path/to/libtrace_pthreads.so
    - RS_THREAD_TRACE_FILE=/path/to/thread_trace.jsonl

    This is set in tools/realsense_thread_trace/run_trace.py:73.

3. Because of LD_PRELOAD, calls to pthread_create, pthread_join, pthread_detach,
    pthread_setname_np, and pthread_exit resolve first to the tracer’s functions in tools/
    realsense_thread_trace/trace_pthreads.c:320.

How Thread Creation Is Traced

When application/librealsense code calls pthread_create:

1. The tracer’s pthread_create() runs first.
2. It gets the real libc function using dlsym(RTLD_NEXT, "pthread_create").
3. It records:
    - timestamp
    - caller Linux TID
    - requested thread entry function
    - argument pointer
    - parent call stack via backtrace()

4. Instead of passing the original start function directly to libc, it passes its own
    trace_start_trampoline.
    See tools/realsense_thread_trace/trace_pthreads.c:343.

That trampoline runs inside the new thread:

trace_emit_thread_start(&ctx);
void *retval = ctx.start_routine(ctx.arg);
trace_emit_thread_exit("return", retval);

See tools/realsense_thread_trace/trace_pthreads.c:291.

So each successfully wrapped thread gets:

- pthread_create: parent-side creation event
- thread_start: child-side actual start event, with real Linux TID
- thread_exit: child-side actual exit event when the start routine returns

Important detail: thread_start can appear in the file before pthread_create, because the
child can begin running before the parent finishes logging the create event. The
timestamps and create_timestamp_ns are used to match them.

How Thread Quit Is Traced

There are two quit-related mechanisms:

1. Normal return from the thread entry function:
    - The trampoline calls the original function.
    - When it returns, the tracer writes thread_exit with exit_kind: "return".
    - See tools/realsense_thread_trace/trace_pthreads.c:302.

2. Explicit pthread_exit():
    - The tracer also interposes pthread_exit.
    - It emits thread_exit with exit_kind: "pthread_exit" before calling the real
      function.

    - See tools/realsense_thread_trace/trace_pthreads.c:455.

pthread_join() is traced separately. It does not prove the thread quit by itself; it
records who waited for the thread and how long the wait took:

- pthread_join_begin
- pthread_join_end

See tools/realsense_thread_trace/trace_pthreads.c:366.

So the best lifecycle interpretation is:

pthread_create  -> requested creation from parent
thread_start    -> child actually started
thread_exit     -> child actually quit
pthread_join_*  -> another thread waited/reaped it

How It Becomes Summary/Timeline

tools/realsense_thread_trace/parse_trace.py:47 reads thread_trace.jsonl and builds thread
records.

It matches records using:

- pthread_value
- create_timestamp_ns
- Linux tid

This matters because pthread_t values are reused, especially for short-lived libusb
threads.

It writes tools/realsense_thread_trace/output/thread_summary.csv:1, with fields like:

- created_ms
- started_ms
- exited_ms
- observed_lifetime_ms
- joined_by
- detached_by
- status

tools/realsense_thread_trace/symbolize.py:42 then uses llvm-symbolizer or addr2line to
map module offsets back to functions/source lines. It also infers the creator site by
filtering out tracer/libstdc++ frames.

RealSense-Specific Meaning

The probe in src/realsense_thread_lifecycle_probe.cpp:75 drives
a normal lifecycle:

context
query_devices
pipeline construction
pipeline.start
wait_for_frames
pipeline.stop
object destruction
process exit

It writes phase markers using include/rs_camera/trace_marker.h:76, so the
timeline can show when threads are created/exited relative to pipeline.start() and
pipeline.stop().

The important RealSense thread sources are:

- dispatcher: creates one std::thread that drains a queue, in deps/librealsense/third-
  party/rsutils/src/dispatcher.cpp:14. Destructor stops and joins it at deps/
  librealsense/third-party/rsutils/src/dispatcher.cpp:47.

- active_object: wraps a dispatcher and repeatedly requeues work, in deps/librealsense/
  third-party/rsutils/include/rsutils/concurrency/concurrency.h:406.

- udev_device_watcher: uses active_object for hotplug polling, in deps/librealsense/src/
  linux/udev-device-watcher.cpp:93.

- usb_context: libusb_init() may create short-lived libusb_event threads, and
  start_event_handler() can create an explicit event thread, in deps/librealsense/src/
  libusb/context-libusb.cpp:13.

- v4l_uvc_device: stream_on() creates the capture thread, and stop_data_capture() signals
  and joins it, in deps/librealsense/src/linux/backend-v4l2.cpp:1652.

For the D435-style trace, the most important long-lived threads are usually the V4L2
capture threads created during pipeline.start() and exiting around pipeline.stop().

Limits

This traces only user-space pthreads inside the launched process. It does not see kernel
UVC workers, IRQ threads, firmware work, external processes, or threads created by non-
pthread mechanisms. It also does not explain why a thread stopped by itself; it records
the exact lifecycle, and the reason is inferred from the RealSense source and phase
markers.

## Worked D435 Trace Interpretation

The successful trace originally recorded under
`tools/realsense_thread_trace/output/` demonstrates the complete mechanism. The
tracer does not modify librealsense. It interposes on pthread APIs through
`LD_PRELOAD`, while the probe emits phase markers that align thread lifetimes
with the RealSense lifecycle.

The preload library records `pthread_create`, `pthread_join`,
`pthread_detach`, `pthread_setname_np`, and `pthread_exit`. Its
`pthread_create` wrapper records the parent TID, timestamp, entry function, and
call stack, then starts `trace_start_trampoline`. The trampoline emits
`thread_start`, invokes the original start routine, and emits `thread_exit`
when that routine returns. See `tools/realsense_thread_trace/trace_pthreads.c`.

The probe marks context construction, device enumeration, pipeline
construction, `pipeline.start()`, first frame, steady state, `pipeline.stop()`,
object destruction, and process exit. See
`src/realsense_thread_lifecycle_probe.cpp` and
`include/rs_camera/trace_marker.h`.

### Recorded startup timeline

The main thread in this trace was TID 57517 and the device was an Intel
RealSense D435.

| Time | Phase |
|---:|---|
| 0.000 ms | `process_start` |
| 0.002 ms | `before_context` |
| 6.745 ms | `after_context` |
| 6.750 ms | `before_query_devices` |
| 19.930 ms | `after_query_devices` |
| 25.865 ms | `before_pipeline_start` |
| 46.988 ms | `after_pipeline_start` |
| 692.275 ms | `first_frame` |
| 1658.749 ms | `steady_state_begin` |
| 10632.019 ms | `before_pipeline_stop` |
| 10643.333 ms | `after_pipeline_stop` |

### Thread-by-thread interpretation

Many threads appeared as `rs-trace-main` because a child initially inherits the
process name. The symbolized call stack, creation phase, and lifetime provide a
more reliable role identifier than the initial name.

| TID | Created | Lifetime | Interpreted role |
|---:|---:|---:|---|
| 57517 | 0 ms | Whole run | Probe main thread: context, query, start, frame wait, stop, and destruction |
| 57518 | 0.210 ms | 11.8 s | `udev_device_watcher` dispatcher for hotplug events |
| 57519 | 3.301 ms | 3.2 ms | Short-lived `libusb_event` thread created during libusb initialization |
| 57520 | 6.818 ms | 3.2 ms | Short-lived `libusb_event` thread created during `query_devices()` |
| 57521 | 11.544 ms | 7.9 ms | Temporary `time_diff_keeper` dispatcher for timestamp calibration |
| 57522 | 12.594 ms | 6.9 ms | Depth raw `uvc_sensor` notification dispatcher |
| 57523 | 13.661 ms | 6.0 ms | Depth synthetic-sensor notification dispatcher |
| 57524 | 15.096 ms | 4.3 ms | Temporary `polling_error_handler` active object |
| 57525 | 17.034 ms | 2.7 ms | Color raw `uvc_sensor` notification dispatcher |
| 57526 | 18.089 ms | 1.7 ms | Color synthetic-sensor notification dispatcher |
| 57527 | 20.019 ms | 3.0 ms | Short-lived `libusb_event` thread used for device information queries |
| 57528 | 25.057 ms | 11.6 s | Pipeline dispatcher, created with the pipeline and destroyed during shutdown |
| 57529 | 25.978 ms | 3.3 ms | Short-lived `libusb_event` thread used while resolving the pipeline profile |
| 57530 | 30.858 ms | 11.7 s | Long-lived `time_diff_keeper` that updates hardware-to-system time mapping |
| 57531 | 32.050 ms | 11.7 s | Long-lived depth raw-sensor notification dispatcher |
| 57532 | 33.128 ms | 11.7 s | Long-lived depth synthetic-sensor notification dispatcher |
| 57533 | 34.447 ms | 11.7 s | Long-lived `polling_error_handler` for device monitoring |
| 57534 | 36.276 ms | 11.7 s | Long-lived color raw-sensor notification dispatcher |
| 57535 | 37.319 ms | 11.7 s | Long-lived color synthetic-sensor notification dispatcher |
| 57536 | 42.945 ms | 10.6 s | V4L2 capture thread, most likely the color UVC stream |
| 57537 | 45.771 ms | 10.6 s | V4L2 capture thread, most likely the depth multi-pin UVC stream |

TIDs 57536 and 57537 are the frame-capture threads. They are created during
`pipeline.start()` and execute a capture loop that waits for a V4L2 file
descriptor, dequeues a completed buffer, synchronizes video and metadata,
delivers the frame upward, and returns the buffer to the driver. Relevant code
is in `deps/librealsense/src/linux/backend-v4l2.cpp` around the capture,
`poll()`, and buffer-handling paths.

This interpretation remains limited to user-space pthreads in the traced
process. Kernel UVC workers, IRQ threads, DMA completion, camera firmware,
external processes, and threads created through other mechanisms are outside
its scope.

## Linux D435 USB and V4L2 Data Path

### 0. The D435 is a composite device

A D435 is physically one camera but exposes several USB interfaces. Those
interfaces carry depth, infrared, color, metadata, and control functions. Linux
therefore sees one USB device with multiple functional interfaces rather than a
single simple video endpoint.

A useful hierarchy is:

```text
D435 USB device
|-- depth and infrared video interfaces
|-- color video interface
|-- metadata interfaces
`-- vendor-specific control interface
```

The exact interface numbers depend on the product and firmware. They must be
discovered from descriptors and sysfs instead of being hard-coded.

### 1. Physical connection and xHCI detection

When the camera is connected, the xHCI host controller detects a port-state
change and notifies the Linux USB core. At this point neither librealsense nor a
`/dev/videoX` node is involved. The system only knows that a USB device has
appeared on a port.

### 2. USB enumeration and descriptors

The USB core assigns a temporary bus address and requests descriptors. These
descriptors identify the vendor, product, serial number, configurations,
interfaces, alternate settings, and endpoints. An endpoint is the actual USB
transport channel; an interface groups endpoints into one function.

Useful inspection commands include:

```bash
lsusb
lsusb -v -d 8086:0b07
```

A typical `lsusb` row reports a bus number, temporary device number, vendor ID,
product ID, and product name. The device number may change after reconnect or
reset; the physical port path and serial number are more stable identifiers.

### 3. Interfaces inside one USB device

A USB interface is an independently bindable function within a device. A
composite camera can expose several interfaces that all share one physical USB
parent. Video-class interfaces advertise the USB Video Class and can be bound
to the standard `uvcvideo` driver. Vendor-specific interfaces may be handled by
librealsense control code or another kernel driver.

### 4. Kernel driver binding

Linux matches every interface against available drivers. Common examples are
`uvcvideo` for USB cameras, `usbhid` for HID devices, `usb-storage` for mass
storage, and `snd-usb-audio` for USB audio.

For the D435 video interfaces, the relevant binding is normally `uvcvideo`.
The topology and active driver can be inspected with:

```bash
lsusb -t
```

This command also reports the negotiated USB speed. High-bandwidth D435
profiles should use SuperSpeed rather than a USB 2 high-speed fallback.

### 5. V4L2 as the Linux video abstraction

The Video4Linux2 subsystem gives applications a common API for video devices.
`uvcvideo` translates V4L2 operations into USB Video Class requests and stream
transfers. This separation allows librealsense to use file descriptors and
ioctls rather than programming the xHCI controller directly.

### 6. `/dev/videoX` nodes

A path such as `/dev/video4` is a character-device node, not a regular file that
stores image data. Opening it connects the process to a kernel V4L2 device.
`read()`, `mmap()`, `poll()`, and `ioctl()` on that descriptor are implemented
by the driver.

Useful commands include:

```bash
ls -l /dev/video*
v4l2-ctl --list-devices
v4l2-ctl --device /dev/video4 --all
```

One physical D435 commonly creates several `/dev/videoX` nodes. These nodes are
functions or pins of one camera, not separate cameras.

### 7. `/sys` versus `/dev`

`/dev` provides handles through which applications operate devices. `/sys`
exposes kernel object relationships and attributes. For a video node, sysfs can
show its USB parent, interface number, driver, bus path, vendor ID, product ID,
and other identity information.

For example:

```bash
readlink -f /sys/class/video4linux/video4/device
```

Walking upward from that path reaches the composite USB parent. This is how the
benchmark maps a RealSense physical port to the correct parent for `usbreset`.

### 8. udev naming, permissions, and events

udev reacts to kernel device-add and device-remove events. It creates or
configures `/dev` nodes, applies permissions and groups, and creates stable
links such as `/dev/v4l/by-id/`. librealsense also watches udev events so that
its context can report camera hotplug and removal.

Useful inspection commands include:

```bash
udevadm info --query=all --name=/dev/video4
udevadm monitor --kernel --udev
```

### 9. librealsense device discovery

`rs2::context::query_devices()` asks the Linux backend to enumerate candidate
video and USB devices. The backend scans video nodes and sysfs/udev metadata,
opens candidates, queries capabilities and formats, checks USB identity, and
groups related nodes into logical RealSense devices.

Discovery does not normally start image streaming. It builds an inventory that
can later be used to resolve and open a pipeline configuration.

### 10. `ioctl()` as the control channel

An ioctl sends a device-specific operation through an open file descriptor.
V4L2 defines a standard family of ioctls for querying capabilities, enumerating
formats, selecting a profile, allocating buffers, and controlling streaming.
The kernel validates and dispatches each request to the V4L2 and UVC driver
implementation.

### 11. `VIDIOC_QUERYCAP`

`VIDIOC_QUERYCAP` reports what a video node can do. Important capability bits
include video capture, metadata capture, and streaming I/O. It also reports the
driver and card names. librealsense uses this information to reject unrelated
or incompatible nodes.

A node can be a candidate only after both its V4L2 capabilities and its USB
identity are understood.

### 12. Video capture capability

A video-capture node delivers image frames to userspace. For a D435, examples
include Z16 depth, Y8 infrared, and RGB or YUYV color. Supported resolutions,
pixel formats, and frame intervals can be inspected with:

```bash
v4l2-ctl --device /dev/video4 --list-formats-ext
```

The existence of a node does not imply that every requested combination of
format, size, and frame rate is valid.

### 13. Streaming capability

The V4L2 streaming API uses a pool of reusable buffers rather than copying each
frame through ordinary file reads. The application allocates or imports
buffers, maps them, queues empty buffers to the driver, starts streaming,
waits for completed buffers, dequeues them, and queues them again.

This buffer pool is central to both throughput and timing behavior.

### 14. Metadata capture

Some RealSense video pins have companion metadata nodes. Metadata can contain
hardware timestamps, frame counters, exposure information, and other per-frame
state. librealsense must distinguish image nodes from metadata nodes and pair
them correctly. A metadata node should not be treated as an independent camera
stream.

### 15. Checking USB identity as well as V4L2 capability

V4L2 capabilities alone say that a node is a video device; they do not prove
that it belongs to a supported RealSense model. librealsense follows sysfs or
udev relationships to obtain the USB vendor ID, product ID, serial number,
physical port, and interface number. It then compares these values with its
supported-device tables.

### 16. Grouping multiple nodes into one logical D435

Nodes that resolve to the same composite USB parent and serial number belong to
the same physical camera. Interface numbers, formats, names, and metadata links
identify their roles. librealsense groups the resulting depth, infrared, color,
and metadata nodes into one logical D435 object.

Consequently, four `/dev/videoX` paths do not mean four cameras. They may be
four functions of one camera.

### 17. What `query_devices()` retains

The discovery result contains device identity, serial and physical-port data,
sensor objects, video-node associations, and supported stream profiles. This
is similar to building an inventory: it records which cameras are present and
what they can support, without yet requesting a continuous frame stream.

### 18. What changes at `pipeline.start()`

`pipeline.start()` resolves the requested configuration, selects a device,
opens the required sensor nodes, chooses compatible profiles, configures V4L2,
allocates and queues buffers, starts the UVC streams, creates capture and
processing threads, and begins delivering frames to the pipeline.

This is why startup creates both short-lived setup threads and long-lived
periodic or event-driven threads.

### 19. Selecting format, resolution, and frame rate

The backend uses V4L2 format and stream-parameter ioctls to request values such
as Z16 depth at 848x480 and 30 FPS. The driver may accept, adjust, or reject the
request. A profile that is valid for one combination of concurrently enabled
streams may fail when USB bandwidth or sensor constraints change.

### 20. Allocating and mapping buffers

The application requests a buffer pool, queries each buffer, and maps its memory
into userspace. Conceptually, these buffers are reusable containers shared
between the driver and application:

```text
application owns empty buffer
        -> QBUF
kernel/driver owns buffer while USB data arrives
        -> DQBUF
application owns completed frame
        -> process and QBUF again
```

The driver cannot continue indefinitely if all buffers remain owned by the
application.

### 21. `STREAMON`

`VIDIOC_STREAMON` activates the configured queue. The UVC driver submits USB
requests, the camera begins delivering payloads, and completed data is assembled
into V4L2 buffers. Starting several D435 streams therefore activates multiple
USB interfaces and consumes host-controller bandwidth concurrently.

### 22. librealsense capture loops

A long-lived capture thread waits for completed V4L2 buffers. When a buffer is
ready, the thread dequeues it, validates status, combines video and metadata,
wraps the memory in librealsense frame objects, invokes upper-layer callbacks,
and returns the buffer.

A simplified loop is:

```text
while streaming:
    wait for a ready file descriptor
    dequeue a completed buffer
    associate metadata and timestamps
    publish the frame
    return the buffer to the driver
```

### 23. Why capture threads sleep in `poll()` or `select()`

Busy polling would waste a CPU while the camera is between frames. `poll()` or
`select()` blocks the capture thread until the kernel reports data or an error.
The thread is therefore sleeping for most of a frame period, becomes runnable
when I/O completes, executes a short processing burst, and blocks again.

This event-driven pattern is important when deriving execution time, period,
and deadline parameters.

### 24. `DQBUF` and `QBUF`

`QBUF` transfers an empty buffer to the driver. After USB transfers fill it,
`DQBUF` transfers the completed buffer back to userspace. Processing must return
buffers quickly enough to maintain a healthy queue depth.

The steady-state cycle is:

```text
QBUF empty buffer
USB/UVC fills buffer
DQBUF completed buffer
process and publish frame
QBUF buffer again
```

### 25. Consequences of a slow application or capture thread

If userspace does not run promptly, dequeue and requeue operations are delayed.
The pool can lose free buffers, queueing delay can grow, and frames may be
stale, dropped, or blocked. CPU contention, long non-preemptible sections,
memory pressure, USB contention, and delayed callbacks can all contribute.

This is one of the central real-time risks: a delayed capture path can propagate
into pipeline latency even when average CPU utilization is low.

### 26. Wrapping V4L2 buffers as `rs2::frame`

librealsense associates the low-level buffer with stream identity, format,
width, height, stride, frame number, timestamps, metadata, and reference-counted
lifetime management. It then exposes the result as an `rs2::frame` without
requiring the application to operate V4L2 directly.

### 27. The role of the pipeline

Without `rs2::pipeline`, an application would need to discover devices, select
sensors and profiles, start several streams, receive callbacks, manage queues,
and synchronize depth and color frames. The pipeline provides this coordination
and emits framesets, while the lower layers still depend on UVC, V4L2, capture
threads, dispatchers, and buffer queues.

### 28. Complete end-to-end sequence

1. The D435 is connected to an xHCI-controlled USB port.
2. The USB core enumerates descriptors and assigns a temporary address.
3. Linux binds `uvcvideo` to the video-class interfaces.
4. V4L2 registers the corresponding video and metadata devices.
5. udev configures nodes, permissions, stable links, and hotplug events.
6. librealsense scans candidate `/dev/videoX` nodes.
7. It queries V4L2 capabilities, formats, sizes, and frame intervals.
8. It follows sysfs/udev relationships to obtain USB identity and topology.
9. It groups nodes that belong to the same physical D435.
10. `query_devices()` exposes the resulting logical device inventory.
11. `pipeline.start()` resolves the requested stream configuration.
12. The backend opens and configures the required V4L2 nodes.
13. It allocates, maps, and queues the streaming buffers.
14. `STREAMON` activates UVC transfers.
15. The camera sends video and metadata over USB.
16. Capture threads wait, dequeue buffers, and publish frames.
17. Pipeline processing synchronizes stream frames into framesets.
18. The application receives frames and eventually returns their buffers.

### 29. Minimal mental model

| Layer | Responsibility |
|---|---|
| USB descriptors | Describe device identity, interfaces, and endpoints |
| xHCI and USB core | Transport requests and payloads between host and camera |
| `uvcvideo` | Implement USB Video Class streaming in the kernel |
| V4L2 | Provide standard video capabilities, configuration, and buffer queues |
| `/dev/videoX` | Expose operational character-device handles to userspace |
| sysfs | Expose topology, identity, interface, and driver relationships |
| udev | Configure nodes, permissions, stable names, and hotplug events |
| librealsense backend | Discover and group nodes, configure sensors, and receive frames |
| capture/dispatcher threads | Move work between I/O completion and pipeline processing |
| `rs2::pipeline` | Resolve streams, coordinate queues, and synchronize framesets |

### 30. Connection to the RTNS timing study

The end-to-end timing path crosses several scheduling and resource domains:

| Domain | Timing risk |
|---|---|
| USB host controller, DMA, and IRQ handling | Transfer completion can be delayed by bus contention or interrupt latency |
| UVC and V4L2 kernel paths | Buffer completion and wakeup latency can vary |
| Capture threads | Runnable delay can postpone `DQBUF`, callbacks, and `QBUF` |
| librealsense dispatchers and pipeline queues | Scheduling and queue interference can add software latency |
| Application callbacks | Slow consumers can retain buffers and increase frame age |
| Camera firmware | Internal production and timestamp behavior is not controlled by Linux scheduling |

The research question is therefore not merely that librealsense creates many
threads. A D435 frame traverses USB, kernel drivers, V4L2 queues, librealsense
threads, and pipeline queues before reaching the application. Contention or
latency at any layer can increase response time or cause frame loss.

LiME characterizes user-thread execution, sleep, and ready intervals. Timerlat
or equivalent kernel tools characterize scheduler and interrupt latency. Frame
numbers and timestamps detect missing or stale frames. USB topology and
background-I/O experiments expose host-controller contention. Together these
measurements support a resource-aware timing model and provide evidence for
selective real-time scheduling rather than assigning one policy to every
librealsense thread.
