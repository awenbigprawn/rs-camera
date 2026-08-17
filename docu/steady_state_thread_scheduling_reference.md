# Librealsense Steady-State Threads and Scheduling Reference

## 1. Scope

This reference consolidates the identified steady-state userspace threads,
their functions, activation order, multiplicity, fixed-priority assignment,
and `SCHED_DEADLINE` parameters. It also explains why enabling a second camera
does not duplicate every process thread.

The numeric profiles apply to:

- Raspberry Pi 5 at 1500 MHz;
- Linux `6.12.96-rpi5-rt-btf-uvc16+`;
- librealsense 2.58.3 with the V4L2/`uvcvideo` backend;
- `UVC_URBS=16`;
- two D435 cameras; and
- profiles derived from three 600-s `SCHED_OTHER` traces per workload.

The source profiles are:

```text
tools/realsense_steady_bench/profiles/
  rpi5-6.12.96-rt-btf-uvc16-two-d435-representative-30fps.csv
  rpi5-6.12.96-rt-btf-uvc16-two-d435-stress-60fps.csv
```

The values are empirical engineering parameters, not proven WCET or minimum
interarrival bounds. Runtime is 1.20 times the largest observed logical-job
execution for an equivalent family, while period/deadline is 0.91 times its
smallest observed stable interval, subject to kernel limits.

## 2. Thread Counts

Three different counts must not be confused:

1. **OS userspace thread instances** include the probe's main thread.
2. **Modeled workers** are live steady workers that receive a scheduling
   profile; main is excluded because it blocks in the control path.
3. **Worker families** merge equivalent instances with the same normalized
   creator-stack signature and source-level function.

| Configuration | Per-camera workers | Process-wide workers | OS userspace threads | Modeled profile entries |
|---|---:|---:|---:|---:|
| One D435, representative Depth+Color | 10 | 2 | 12 | 11 |
| Two D435, representative Depth+Color | 20 | 2 | 22 | 21 |
| One D435, stress Depth+IR1+IR2+Color | 11 | 2 | 13 | 12 |
| Two D435, stress Depth+IR1+IR2+Color | 22 | 2 | 24 | 23 |

The two process-wide threads are `main` and one udev watcher. Only the udev
watcher receives a real-time worker profile; `main` remains `SCHED_OTHER`.

### 2.1 Why Stress Has Four Depth/IR Capture Instances

The stress Deadline CSV contains four entries for one Depth/IR capture
signature. They are not four depth threads on one camera. They are:

| Instance | Camera | UVC input |
|---:|---|---|
| 1 | Camera 0 | Depth Z16 |
| 2 | Camera 0 | Interleaved infrared Y8I |
| 3 | Camera 1 | Depth Z16 |
| 4 | Camera 1 | Interleaved infrared Y8I |

IR1 and IR2 do not own two capture pthreads. The D435 stereo path delivers one
interleaved Y8I input, and synchronous `y8i_to_y8y8` processing separates it
into logical IR1 and IR2 frames inline. The depth and Y8I workers share the
same source/creator signature, so the profile represents them as four
instances of one family.

## 3. Per-Camera and Shared Threads

One all-stream D435 contributes eleven workers:

| Worker owned by one camera | Count | Activation class | Function |
|---|---:|---|---|
| Pipeline dispatcher | 1 | Event-driven/dormant | Executes asynchronous pipeline control requests |
| Time-difference keeper | 1 | Timer-driven, approximately 1 s | Maintains the device-to-host clock relation |
| Firmware-error poller | 1 | Slow timer-driven | Queries and decodes device error state |
| Raw-depth notification dispatcher | 1 | Event-driven | Serializes raw depth/stereo sensor notifications |
| Synthetic-depth notification dispatcher | 1 | Event-driven | Serializes synthetic depth/stereo notifications |
| Raw-color notification dispatcher | 1 | Event-driven | Serializes raw color-sensor notifications |
| Synthetic-color notification dispatcher | 1 | Event-driven | Serializes synthetic color notifications |
| Depth capture | 1 | Frame-driven | `pselect`/DQBUF, metadata, callback, and buffer requeue for Z16 |
| Interleaved-IR capture | 1 | Frame-driven | Receives Y8I and produces IR1/IR2 inline |
| Color capture | 1 | Frame-driven | DQBUF, metadata, YUYV-to-RGB8 conversion, callback, and requeue |
| Application wait (`rs-wait-N`) | 1 | Frame-driven | Blocks in `wait_for_frames()` and records returned framesets |

The process contributes only one instance of each shared thread:

| Process-wide thread | Count | Activation class | Function |
|---|---:|---|---|
| Main | 1 | Control/blocking | Creates cameras, marks phases, installs profiles, starts/stops, and joins; it does not run the per-camera wait loop |
| udev watcher | 1 | Timer-driven, approximately 100 ms | Monitors hotplug/device changes for the shared context/backend |

Consequently, adding a second all-stream D435 adds eleven workers rather than
thirteen. Main and udev are reused by the process. In the representative
workload, IR is disabled, so each camera contributes ten workers and a second
camera adds ten.

## 4. Causal Activation Order for a Delivered Frameset

A frame does not wake every worker in one fixed total order. The correct model
is a partial order with parallel stream branches:

```text
camera USB packets
  -> xHCI DMA and completion service
  -> USB HI_SOFTIRQ giveback and UVC header/FID/EOF processing
  -> high-priority unbound UVC copy worker
  -> vb2_buffer_done marks a V4L2 buffer ready
  -> the corresponding Depth, Y8I, or Color capture worker wakes
       -> DQBUF
       -> metadata and format-specific processing
       -> sensor callback
       -> QBUF/requeue
  -> synchronization and frameset aggregation run inline on callback paths
  -> the last required stream closes the frameset join
  -> the corresponding rs-wait-N worker wakes
  -> wait_for_frames() returns and the probe records the frameset
```

Depth, IR, and Color branches can execute in different orders. There is no
valid fixed statement that Depth always precedes Color. In one recent D435
60-FPS receive-path trace, Depth/IR inputs usually preceded Color by about
8--9 ms and Color often closed the join, but that is an observation of one
configuration, not an API guarantee.

The pipeline dispatcher, notification dispatchers, firmware poller,
time-difference keeper, and udev watcher are not mandatory sequential stages
of every frameset. They wake from control events, device notifications, or
their own slower timers.

The kernel predecessor is also broader than an xHCI thread. Depending on
kernel configuration it includes hard IRQ or threaded IRQ service, USB
giveback in HI_SOFTIRQ/`ksoftirqd`, and shared unbound UVC copy workers. See
`docu/realsense_kernel_receive_path_execution_contexts.md`.

## 5. Scheduling Table

The table aggregates equivalent instances. `Instances 30/60` is the number of
profile entries for two D435 cameras. RR-RM and FIFO-RM use identical numeric
priorities; only their scheduling class differs. A shorter modeled period
receives a higher priority, and equal periods receive equal priority.

| Worker family | Instances 30/60 | RM priority 30/60 | Deadline runtime 30 FPS | Deadline D=T 30 FPS | Deadline runtime 60 FPS | Deadline D=T 60 FPS | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Depth/IR capture | 2 / 4 | 78 / 80 | 636.414 us | 29.895593 ms | 1783.446 us | 12.229092 ms | Depth only at 30 FPS; Depth and Y8I instances at 60 FPS |
| Color capture | 2 / 2 | 79 / 78 | 1661.123 us | 29.655501 ms | 2882.306 us | 12.470306 ms | Includes RGB8 conversion on the callback path |
| Raw-depth notification | 2 / 2 | 75 / 75 | 100 us | 4194.304 ms | 100 us | 4194.304 ms | Event queue; normally not a per-frame callback |
| Synthetic-depth notification | 2 / 2 | 75 / 75 | 100 us | 4194.304 ms | 100 us | 4194.304 ms | Event queue |
| Raw-color notification | 2 / 2 | 75 / 75 | 100 us | 4194.304 ms | 100 us | 4194.304 ms | Event queue |
| Synthetic-color notification | 2 / 2 | 75 / 75 | 100 us | 4194.304 ms | 100 us | 4194.304 ms | Event queue |
| Pipeline dispatcher | 2 / 2 | 75 / 75 | 100 us | 4194.304 ms | 100 us | 4194.304 ms | Asynchronous control queue |
| Firmware-error poller | 2 / 2 | 75 / 75 | 100 us | 4194.304 ms | 100 us | 4194.304 ms | Observed slow polling; period clamped to kernel maximum |
| udev watcher | 1 / 1 | 77 / 77 | 100 us | 91.021918 ms | 100 us | 91.035477 ms | Process-wide hotplug poller |
| Time-difference keeper | 2 / 2 | 76 / 76 | 263.421 us | 910.640845 ms | 608.753 us | 910.646585 ms | Per-camera clock mapping; 60-FPS runtime refined by gated validation |
| Application wait (`rs-wait-N`) | 2 / 2 | 80 / 79 | 100 us | 27.673357 ms | 168.394 us | 12.243126 ms | One consumer per camera |
| Main | 1 / 1 | OTHER / OTHER | Not assigned | Not assigned | Not assigned | Not assigned | Blocks during steady state and controls phase transitions |
| xHCI IRQ service | 2 / 2 when threaded | treatment-specific | Not modeled as Deadline | Not modeled | Not modeled as Deadline | Not modeled | RR-RM uses RR 90; FIFO-RM and SDK Deadline use FIFO 90 in coordinated treatments |

The 4194.304-ms value is the configured kernel maximum accepted by the profile
generator. The corresponding workers were observed at approximately 5 s or
as event-driven/dormant; 4194.304 ms is not a claim that their true physical
period is exactly that value.

### 5.1 Rate-Monotonic Priority Bands

The implementation starts at priority 80 for SDK workers and assigns one
descending priority per distinct modeled period. Equal periods share a level.

At 30 FPS:

```text
27.673357 ms  -> 80  rs-wait
29.655501 ms  -> 79  Color capture
29.895593 ms  -> 78  Depth capture
91.021918 ms  -> 77  udev watcher
910.640845 ms -> 76  time-difference keeper
4194.304 ms   -> 75  slow/event-driven services
```

At 60 FPS:

```text
12.229092 ms  -> 80  Depth/Y8I capture
12.243126 ms  -> 79  rs-wait
12.470306 ms  -> 78  Color capture
91.035477 ms  -> 77  udev watcher
910.646585 ms -> 76  time-difference keeper
4194.304 ms   -> 75  slow/event-driven services
```

RR-RM and FIFO-RM differ in equal-priority execution semantics, not in this
priority mapping. The xHCI IRQ treatment uses priority 90 so that the kernel
predecessor is not delayed behind SDK work. For SDK Deadline experiments, xHCI
still uses FIFO 90 because its sporadic burst demand is poorly represented by
a constant-bandwidth Deadline reservation.

## 6. Interpretation Limits

- The profile contains thread instances, not one independent family per CSV
  row. Equivalent instances deliberately share parameters.
- Notification and pipeline workers can be alive but blocked. Their profile
  entries do not imply one activation per frame.
- `SCHED_DEADLINE` parameters are empirical margins over finite traces, not
  certified worst-case bounds.
- The capture profiles account for librealsense workers, not complete kernel
  receive-path CPU demand.
- Two cameras share process-wide userspace services and kernel workqueue/
  softirq infrastructure. Multi-camera scaling is therefore not a literal
  multiplication of every thread.
- The thread signatures depend on the binary build and source version. Keep
  `RelWithDebInfo` consistent across calibration and evaluation so that
  normalized module offsets and source interpretation remain comparable.

## 7. Related Documentation

- `docu/realsense_temporal_and_resource_model_tutorial.md`
- `docu/realsense_kernel_receive_path_execution_contexts.md`
- `docu/pthread_trace_and_phase_markers.md`
- `docu/xhci_irq_deadline_calibration_20260811.md`
- `tools/realsense_steady_bench/README.md`
