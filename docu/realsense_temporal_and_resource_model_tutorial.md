# RealSense Temporal and Resource Model: A Step-by-Step Tutorial

This document explains the model in the RTNS paper's `body/20-models.tex`. It
focuses on why each component exists, what every quantity means, where the
data originate, and how the model informs the experiments and scheduling
design.

## 0. The Four Model Layers

The model is not one large equation. It builds four connected layers:

| Layer | Model | Question | Main output |
|---|---|---|---|
| 1 | Start-phase graph `G_S` | How is the pipeline established? | Creation, first execution, exit, and precedence |
| 2 | Steady-state graph `G_P` | Which work repeats after startup? | Frame-, timer-, event-driven, and sleeping families |
| 3 | Resource vector `R(W)` | What resources does a stream workload require? | CPU, USB payload, and memory byte touches |
| 4 | Frameset join and freshness | How do several streams form an application frameset? | Delivery, duplicate, gap, and stale outcomes |

Their relationship is:

```text
G_S establishes workers and the data path
  -> after t_S, G_P becomes stable
  -> G_P activations measure CPU demand; profiles determine USB/memory demand
  -> R(W) describes workload pressure
  -> join/freshness describes the application-visible consequence of delay
```

The objective is not a schedulability theorem. It decomposes an apparently
simple `wait_for_frames()` call into components that can be identified,
measured, configured, and validated.

## 1. A Camera Is Not One Periodic Task

The application interface looks simple:

1. construct a pipeline;
2. call `pipeline.start()`;
3. call `wait_for_frames()` repeatedly;
4. consume a frameset; and
5. stop the pipeline.

It is therefore tempting to model a 30-FPS camera as one 33.3-ms periodic
task. The actual path is:

```text
camera firmware
    -> USB transmission
    -> xHCI completion / IRQ service
    -> uvcvideo assembles a V4L2 buffer
    -> librealsense capture worker
    -> metadata and stream callback
    -> stream synchronization / frameset join
    -> output queue
    -> application wait thread
```

The path contains userspace workers, kernel execution contexts, DMA, USB, and
shared-memory traffic. Activations also have different causes. Some follow
frames, some wake at approximately 100 ms or 1 s, some respond only to an
event, and some remain alive without waking during a complete run.

The resulting abstraction is a phase-aware, multi-rate, self-suspending task
graph rather than one periodic task.

## 2. Why Execution Has Two Phases

### 2.1 Start Phase

The start phase lasts from process start until both conditions hold:

- the application has received its first complete frameset; and
- every recurring worker that must execute periodically has entered a stable
  activation pattern.

This phase enumerates devices, selects profiles, configures sensors, creates
V4L2 buffers, starts streams, and creates workers. The thread set still
changes, and temporary workers can exit before the first frameset.

It is therefore finite work with precedence constraints, not a periodic task.

### 2.2 Steady State

After the transition:

- camera configuration no longer changes;
- the persistent thread set is stable;
- frame-driven workers execute repeatedly; and
- timer- and event-driven services continue according to their own triggers.

Only this phase supports meaningful estimates of periods, execution times,
Deadline runtimes, and fixed priorities.

## 3. Evidence Sources

A worker's function and temporal behavior come from three complementary
sources.

### 3.1 Source Inspection

Source code identifies functions such as:

- V4L2 capture;
- timestamp maintenance;
- firmware-error polling;
- notification dispatch; and
- application `wait_for_frames()`.

### 3.2 pthread Lifecycle Interposer

The local `libtrace_pthreads.so` interposes `pthread_create()`, entry, naming,
exit, and join and records the creator stack. A normalized stack signature
identifies a worker family. This is necessary because several librealsense
workers have identical or truncated Linux names.

### 3.3 LiME Scheduler Trace

LiME records when a thread:

- executes on a CPU;
- is runnable but not executing;
- sleeps or blocks;
- wakes again; and
- exits.

The pthread trace answers *which runtime worker is this?* LiME answers *when
did it execute?* Source inspection answers *why does it execute?* The task
model requires all three.

## 4. Start Phase: A Finite Precedence Graph

The start phase is represented by a directed acyclic graph:

```text
G_S = (V_S, E_S)
```

A vertex is a one-shot operation or the initialization work of a persistent
thread. An edge means that one operation must precede another, or that a
thread creates another thread. A simplified path is:

```text
context
  -> device enumeration
  -> device construction
  -> stream profiles
  -> stream-on
  -> capture workers
  -> first stream inputs
  -> first complete frameset
```

Color and depth/infrared initialization can proceed partly in parallel, so the
complete graph is not one chain.

### 4.1 Per-Thread Start Times

For thread instance `j`, the trace records:

- `c_j`: creation;
- `s_j`: first actual CPU execution;
- `e_j`: exit, when the thread terminates; and
- `q_j`: first stable activation pattern for a persistent recurring worker.

These quantities identify when a worker exists, how long it waits after
creation, whether it is startup-only, and when a persistent worker becomes
temporally repeatable.

### 4.2 `pipeline.start()` Is Not the End of Startup

Three times have different meanings:

- return from `pipeline.start()`: configuration call completed;
- `t_F`: first complete frameset reached the application; and
- `t_conv`: the last recurring family entered a stable pattern.

```text
t_conv = max(q_i), over recurring workers i
t_S = max(t_F, t_conv)
```

Receiving one frameset is insufficient if part of the steady task set has not
yet converged. The 20 start-phase processes validate the worker structure,
creation order, and boundary; they are not the source of long-run frame-period
estimates.

## 5. Steady State: A Multi-Rate, Self-Suspending Graph

Steady state uses:

```text
G_P = (V_P, E_P)
```

Its edges represent activation or data dependencies rather than thread
creation:

```text
xHCI completion
  -> V4L2 buffer ready
  -> capture callback
  -> stream join
  -> application wait returns
```

### 5.1 Four Activation Classes

#### Frame-driven

Capture workers, inline callbacks, and application wait workers advance with
camera frames. Their principal interval approaches `1 / FPS`, with jitter
from USB, buffering, and scheduling.

#### Timer-driven

Timestamp maintenance and firmware checks use rates independent of frame FPS,
such as approximately 100 ms, 1 s, or slower. Assigning the camera frame
period to these workers would be incorrect.

#### Event-driven

Notification and queue workers wait for a device or software event. A fixed
period cannot be claimed without enough observed events.

#### Sleeping service

Some persistent service workers never wake during a measurement window. A
long lifetime does not imply periodic CPU demand.

### 5.2 Per-Family Tuple

The model uses:

```text
tau_i = (C_i, A_i, D_i)
```

- `C_i`: total CPU time in one logical activation;
- `A_i`: frame, timer, or event activation source; and
- `D_i`: relative deadline.

A periodic family additionally has `T_i`. An event-driven family may receive
an empirical minimum interarrival `Delta_i` when enough events exist, but it
must not receive a fabricated period for convenience.

### 5.3 Self-Suspension

One logical V4L2 activation can contain:

```text
run -> block in pselect/DQBUF -> run callback -> block -> run requeue
```

For `q` CPU bursts:

```text
C_i = C_i,1 + C_i,2 + ... + C_i,q
```

Blocked wall time affects response time but is not CPU execution. Deadline
runtime must not be the complete elapsed time from wakeup to the next sleep.

## 6. CPU Resource Model

For workload `W`, define:

- `F(W)`: worker-family set;
- `n_i`: equivalent instances of family `i`;
- `C_hat_i`: maximum logical execution across calibration runs and equivalent
  instances; and
- `T_hat_i`: minimum stable interval in the same data.

The conservative observed demand is:

```text
U_cpu_obs = sum_i n_i * C_hat_i / T_hat_i
```

The unit is a CPU-core equivalent. `U=0.8` represents demand equal to 80% of
one core, but does not require all tasks to execute on one particular core.
This is not mean utilization: combining each family's cross-run maximum `C`
and minimum `T` is deliberately more conservative.

### 6.1 Example

Suppose two cameras each have one equivalent capture worker. Across three
traces their largest logical executions are 0.42 and 0.50 ms, and shortest
stable periods are 33.1 and 32.9 ms. A shared conservative family profile is:

```text
C_hat = max(0.42, 0.50) = 0.50 ms
T_hat = min(33.1, 32.9) = 32.9 ms
n = 2
```

Its demand is:

```text
n * C_hat / T_hat = 2 * 0.50 / 32.9 = 0.0304 cores
```

The complete workload sums every family. Multiplicity matters: a profile for
one instance would underestimate a multi-camera system.

### 6.2 Deadline Reservation

Formal profiles use:

```text
Q_i = 1.20 * C_hat_i
P_i = D_i = 0.91 * T_hat_i
U_cpu_res = sum_i n_i * Q_i / P_i
```

`Q_i` is runtime, `P_i` reservation period, and `D_i` relative deadline. A
20% runtime increase and 9% period reduction add empirical headroom beyond
the trace extrema. These are engineering margins, not proven WCET or minimum
interarrival bounds.

Ignoring kernel clamps and minimum runtime, the reservation expands observed
demand by:

```text
(1.20 * C_hat / (0.91 * T_hat)) / (C_hat / T_hat)
= 1.20 / 0.91
= 1.319
```

Thus the reservation is about 31.9% above the conservative observed demand.
For representative acquisition, `0.123 * 1.319 = 0.162` cores, close to the
actual 0.164 after per-entry rounding, minimum runtimes, and filtering.

Two-D435 calibration produced:

| Workload | Conservative observed CPU | Deadline reservation |
|---|---:|---:|
| Representative 30 FPS | 0.123 cores | 0.164 cores |
| Stress 60 FPS | 0.815 cores | 1.076 cores |

Stress does more than halve frame periods. It enables two IR streams, adds
capture work, and increases color conversion, yielding approximately 6.6
times the CPU demand.

### 6.3 Exclusions

The original family sum excludes xHCI IRQ and other kernel receive work,
because they are not librealsense workers. A complete host decomposition is:

```text
U_host = U_xhci + U_usb_bh + U_uvc_copy + U_sdk + U_application
```

The kernel components are bursty shared servers and require short-window
demand measurements in addition to long-term utilization. The current SDK
model is not WCET analysis; `C_hat_i` is the largest observed value in finite
traces.

## 7. USB Payload Model

For camera count `N_c`, frame rate `f`, stream dimensions `w_s` and `h_s`, and
wire-format bytes per pixel `b_wire_s`:

```text
B_USB_payload = N_c * f * sum_s(w_s * h_s * b_wire_s)
```

### 7.1 Representative 30 FPS

Per D435:

- Depth Z16: `848 * 480 * 2 * 30` bytes/s; and
- Color YUYV on the wire: `640 * 480 * 2 * 30` bytes/s.

Two cameras produce 81.7 MiB/s. On separate xHCI controllers, each controller
receives approximately 40.9 MiB/s.

### 7.2 Stress 60 FPS

Per D435:

- Depth Z16: 848 x 480 x 2;
- IR1 Y8: 848 x 480 x 1;
- IR2 Y8: 848 x 480 x 1;
- Color YUYV on the wire: 960 x 540 x 2; and
- all streams at 60 FPS.

Two cameras produce 305.0 MiB/s, or approximately 152.5 MiB/s per controller.

These are payload rates rather than USB line rates. UVC headers, protocol
overhead, isochronous scheduling, and gaps consume additional bus time.

## 8. Memory Byte-Touch Model

Each wire payload in the V4L2 path involves at least:

1. an xHCI DMA write to memory;
2. a CPU read from the V4L2 buffer; and
3. a CPU write to a librealsense frame.

The base path therefore touches about `3 * B_USB_payload` bytes. When the D435
requests RGB8, the wire path uses YUYV and librealsense additionally:

4. reads YUYV color; and
5. writes RGB8 color.

```text
B_mem_touch = 3 * B_USB_payload
              + B_color_YUYV
              + B_color_RGB8
```

| Workload | Code-path byte touches |
|---|---:|
| Representative 30 FPS | approximately 333 MiB/s |
| Stress 60 FPS | approximately 1,212 MiB/s |

This is not a memory-controller measurement or a DRAM-bandwidth bound. Caches
can reduce DRAM traffic, while cache-line fetches, writeback, allocation,
metadata, and coherency can increase it. GPU traffic is excluded. The proper
term is a *byte-touch estimate*, not measured memory bandwidth or a strict
admission test.

## 9. Why GPU Work Interferes

Librealsense does not execute frame processing on the GPU, so GPU work is
external interference rather than camera task demand. Raspberry Pi 5 V3D
buffer objects use system memory. V3D, CPU, and RP1/xHCI DMA ultimately share
BCM2712 memory/interconnect service. Continuous inference can affect:

- xHCI DMA memory service;
- V4L2-to-SDK copying;
- YUYV-to-RGB8 conversion;
- CPU cache and memory latency; and
- CPU work for Vulkan submission.

Across 48 main-matrix GPU runs, inference occupied 99.89%--99.996% of the
measurement window, establishing nearly continuous GPU pressure. The system
lacks memory-controller, interconnect, and GPU bandwidth counters, however.
The supported statement is that sustained GPU work introduces combined CPU
and shared-memory-path interference, not that every change is caused solely
by DRAM bandwidth.

## 10. Frameset Precedence

Let `a_c,s,k` be the arrival time of selected stream `s` for camera `c` and
frameset `k`. A frameset cannot complete before its last required stream:

```text
J_c,k = max_s(a_c,s,k)
```

Application delivery additionally includes:

```text
t_app = J + post-join processing + output-queue/application delay
```

CPU scheduling can improve downstream processing but cannot restore a stream
input already lost or rendered incomplete in USB/UVC.

For example, if depth arrives at 100.0 ms and color at 102.0 ms, the join
cannot occur before 102.0 ms. If post-join work takes 0.8 ms and queue/
application service adds 0.5 ms, delivery occurs at 103.3 ms. Raising the
application wait priority does not eliminate the original 2-ms stream skew.

## 11. Freshness

A successful API return does not imply new data. For stream `s`:

```text
u = 1 if current frame number > previous frame number
u = 0 otherwise
```

Definitions:

- **fresh frameset:** every required stream advances;
- **partially stale:** at least one stream advances and at least one is reused;
- **fully stale:** no required stream advances;
- **duplicate:** one stream's frame number does not advance; and
- **sequence gap:** adjacent observed frame numbers differ by more than one.

```text
g = max(0, current_number - previous_number - 1)
```

This separates late delivery, reuse of old data, and unobserved sequence
numbers. The diagnosed incomplete depth input follows:

```text
incomplete UVC depth input
  -> no valid new depth frame reaches the join
  -> another stream advances
  -> join reuses previous depth
  -> partially stale frameset
  -> next valid depth reveals a sequence gap
```

## 12. Scheduling Decisions Derived from the Model

### 12.1 Keep the Start Phase in `SCHED_OTHER`

The start-phase task set changes, and not all persistent workers exist yet.
Giving every inherited temporary discovery/control worker one RR/FIFO priority
does not express steady precedence. Likewise, steady Deadline budgets do not
describe one-shot enumeration work.

```text
recovery/start under OTHER
  -> first frames and persistent workers available
  -> match families by signature
  -> install family-specific policy
  -> continue stabilization
  -> begin measurement
```

### 12.2 RR/FIFO Use Rate-Monotonic Ordering

Workers have different rates, so they must not all receive priority 80. A
shorter observed period maps to a higher priority; equal periods receive equal
priority.

### 12.3 Deadline Uses Per-Family Parameters

Each family receives its own `Q_i`, `P_i`, and `D_i`, not the camera period.
Equivalent workers from two cameras share the most conservative pooled
parameters.

### 12.4 Treat xHCI IRQ Separately

xHCI IRQ service precedes capture and is sporadic and bursty. A Deadline
reservation based on its long-term average cannot cover short bursts without
reserving nearly a full CPU. Coordinated experiments therefore use a high-
priority FIFO IRQ treatment rather than placing xHCI in the userspace model.

## 13. Model Claims and Evidence

| Model claim | Evidence |
|---|---|
| Start-phase task set changes | pthread lifecycle and LiME traces from 20 independent processes |
| Functions can be classified | creator-stack signatures plus librealsense source paths |
| `start()`, first frameset, and convergence differ | start-phase markers |
| Steady state is multi-rate | three 600-s `SCHED_OTHER` calibrations at 30 and 60 FPS |
| Logical activations self-suspend | scheduler run/sleep/wakeup sequences |
| CPU demand can be instantiated per family | maximum `C` and minimum `T` in calibration |
| Profiles determine USB demand | analytical wire-format calculation |
| Profiles determine memory traffic | inspected copy/conversion byte-touch calculation |
| API success does not guarantee freshness | per-stream frame-number analysis |
| UVC failure propagates to freshness | aligned UVC/V4L2/callback diagnostic trace |
| xHCI is a scheduling predecessor | IRQ-policy pilot under CPU contention |
| GPU is shared-resource interference | hardware V3D validation and continuous GPU workload |

## 14. Claim Boundary

Supported:

- librealsense acquisition is not one simple periodic task;
- startup is a finite precedence graph;
- steady state is a multi-rate, self-suspending graph;
- each stream workload requires independent CPU calibration;
- a workload changes CPU, USB payload, and memory byte touches;
- fresh-frame delivery depends on userspace and the kernel USB receive path;
- policies should be installed per persistent family after startup.

Not supported:

- mathematically proven WCET or response-time guarantees;
- strict USB-bus admission;
- hardware-measured DRAM bandwidth from the analytical estimate;
- attribution of all GPU interference to memory bandwidth;
- universal optimality of `UVC_URBS=16`; or
- a claim of 100% long-term reliability from an error-free 30-s run.

## 15. Five-Point Summary

1. The start phase is a one-shot precedence graph, not a periodic task.
2. Steady state is a multi-rate, self-suspending task graph.
3. `C/T` estimates CPU demand, wire formats give USB payload, and inspected
   copies/conversions give memory byte touches.
4. The last required stream determines frameset join; an API return does not
   prove that every stream is fresh.
5. CPU policy controls runnable CPU service only. USB, DMA, memory, xHCI IRQ,
   softirq, and UVC copying remain parts of the acquisition path.

## 16. Additional Validation from 2026-08-12

See `docu/model_validation_supplement_20260812.md` for complete conditions and
results.

First, LiME on/off was repeated three times for both Linux 6.12 kernels and
both workloads. All 24 runs had zero duplicates, gaps, stale framesets, and
timeouts. At 30 FPS, p99 changed by less than 0.2%. At 60 FPS, LiME increased
p99 by approximately 1.5%--3.4% and maxima by approximately 5.9%--7.7%. LiME
did not create the freshness failures under study, although it introduced a
small measurable tail perturbation under high load.

Second, promoting only persistent userspace workers to FIFO-RM was not always
sufficient under four register-only CPU workers. In the PREEMPT_RT 60-FPS
condition, FIFO-RM userspace with xHCI IRQ at OTHER produced 12 duplicates and
23 gaps over three runs. Coordinating xHCI at FIFO 90 reduced both to zero,
p99 from 18.040 to 16.979 ms, and the maximum from 53.193 to 17.465 ms. This
directly validates that the kernel predecessor in `G_P` cannot be omitted from
the scheduling design.

Third, targeted LiME interference traces separated `running` and `ready`.
At 30 FPS, CPU-only interference multiplied ready wait by 29.4 while frame-
family execution p99 rose only 8.8%; rate-limited fixed-copy interference
raised execution p99 by 28.6%. At 60 FPS, CPU-only ready wait rose 10.4 times,
while fixed-copy execution p99 rose 40.6%. The separation between waiting for
CPU and longer execution after dispatch is therefore empirically meaningful.
See `docu/model_interference_mechanism_trace_20260812.md`.

Fourth, one- versus two-camera experiments validated multiplicity `n_i`.
Across two kernels and two workloads, frame-driven instances increased exactly
from 3/4 to 6/8. Total threads rose only to 1.83--1.85 times because process-
wide families were shared. Actual running demand rose 2.12--2.64 times.
Multiplicity describes structure, but one-camera average CPU cannot be scaled
mechanically; formal profiles still require cross-instance maxima on the
target topology. See `docu/model_camera_scaling_validation_20260812.md`.

Fifth, a one-D435 60-FPS stream ablation validated the resource vector. Depth,
Depth+Color, and Depth+Color+IR1+IR2 yielded analytical USB payloads of 46.582,
105.908, and 152.490 MiB/s and byte-touch lower bounds of 139.746, 466.040,
and 605.786 MiB/s. On both kernels, total userspace threads rose from 11 to 12
and 13, while frame-driven instances rose from 2 to 3 and 4. Standard-kernel
running demand rose from 0.022 to 0.130 and 0.211 cores; PREEMPT_RT rose from
0.023 to 0.133 and 0.215 cores. An 18% thread-count increase accompanied an
approximately 9.2--9.6-fold CPU-demand increase, confirming that `n_i`,
`C_i/T_i`, and path byte volume describe distinct dimensions. See
`docu/model_stream_ablation_validation_20260812.md`.
