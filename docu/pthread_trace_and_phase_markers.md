# RealSense pthread Lifecycle Tracing and Phase Markers

This document explains the design and functions of:

- `tools/realsense_thread_trace/trace_pthreads.c`; and
- `include/rs_camera/trace_marker.h`.

Together, these components write thread-lifecycle semantics and application
phase semantics to `thread_lifecycle.jsonl`:

- `trace_pthreads.c` uses `LD_PRELOAD` to interpose pthread APIs without
  modifying librealsense.
- `trace_marker.h` is called explicitly by the probe to mark context creation,
  pipeline start, first delivery, stop, and other application phases.

```text
                        d435_sensor_probe
                               |
             +-----------------+------------------+
             |                                    |
       LD_PRELOAD interposition              Explicit markers
       trace_pthreads.c                      trace_marker.h
             |                                    |
             +-- pthread_create                   +-- process_start
             +-- thread_start                     +-- before_pipeline_start
             +-- thread_exit                      +-- after_pipeline_start
             +-- pthread_join                     +-- first_frame
             +-- pthread_detach                   +-- frames_complete
             +-- thread_name                      +-- process_exit
             |                                    |
             +-----------------+------------------+
                               |
                               v
                    thread_lifecycle.jsonl
                               |
                               | merge with LiME scheduler events
                               v
               thread_timing.csv / thread_intervals.csv
```

## 1. `trace_pthreads.c`

### 1.1 Constants

```c
#define TRACE_MAX_STACK 10
#define TRACE_LINE_SIZE 4096
```

- `TRACE_MAX_STACK` limits a `pthread_create()` creator stack to ten frames.
- `TRACE_LINE_SIZE` is the fixed 4096-byte buffer for one JSONL record.

### 1.2 `trace_start_context`

```c
struct trace_start_context
{
    void *(*start_routine)(void *);
    void *arg;
    pid_t parent_tid;
    uint64_t create_timestamp_ns;
};
```

The parent allocates this context when it creates a thread and passes it to
the tracer trampoline.

| Field | Meaning |
|---|---|
| `start_routine` | Original librealsense entry passed to `pthread_create()` |
| `arg` | Original thread argument |
| `parent_tid` | Kernel TID of the thread that called `pthread_create()` |
| `create_timestamp_ns` | Timestamp immediately before the create call |

The wrapper replaces the original entry with `trace_start_trampoline()`. The
trampoline records the child start and then calls `start_routine(arg)`.

### 1.3 Global and Thread-Local State

```c
static int trace_fd = -1;
static __thread int trace_guard = 0;
static __thread void *trace_tls_entry = NULL;
static __thread uint64_t trace_tls_start_ns = 0;
static __thread pid_t trace_tls_parent_tid = 0;
```

- `trace_fd` is the JSONL output descriptor.
- `trace_guard` is a thread-local recursion guard.
- `trace_tls_entry` stores the current thread's original entry.
- `trace_tls_start_ns` stores the time at which the thread entered the
  trampoline.
- `trace_tls_parent_tid` stores the creator's TID.

Logging, symbol lookup, or allocation may indirectly invoke a pthread API.
The per-thread guard prevents such a call from recursively entering the
wrapper forever.

### 1.4 Pointers to the Real pthread Functions

Because `LD_PRELOAD` makes the program resolve the tracer's `pthread_create()`
first, the tracer obtains the actual libc implementation with:

```c
dlsym(RTLD_NEXT, "pthread_create")
```

It does the same for every intercepted API. Otherwise, a wrapper would call
itself recursively.

## 2. Internal Helpers

### 2.1 `trace_now_ns()`

This function reads `CLOCK_BOOTTIME` with `clock_gettime()` and returns a
nanosecond timestamp. LiME uses `bpf_ktime_get_boot_ns()`, so the two data
sources share a BOOTTIME clock domain and can be merged directly.

The analyzer obtains relative time as:

```text
relative_time = event_timestamp - process_start_timestamp
```

### 2.2 `trace_gettid()`

This helper calls `syscall(SYS_gettid)` to obtain the Linux kernel TID used by
LiME and the scheduler.

- `pthread_self()` returns a userspace `pthread_t`.
- `gettid()` returns a kernel task ID.
- LiME scheduler events identify tasks by the kernel ID.

The trace records both identifiers so that pthread events can be joined with
scheduler events.

### 2.3 `trace_load_real_symbols()`

This helper resolves the real implementations of:

- `pthread_create`;
- `pthread_join`;
- `pthread_detach`;
- `pthread_exit`;
- `pthread_setname_np`; and
- `pthread_getname_np`.

It resolves only missing pointers and is therefore safe to call repeatedly.

### 2.4 `trace_open_file()`

The output path comes from:

```sh
RS_THREAD_TRACE_FILE=/path/to/thread_lifecycle.jsonl
```

If the variable is absent, the default is `thread_trace.jsonl`. The file is
opened with `O_APPEND | O_CREAT | O_WRONLY | O_CLOEXEC`:

- append rather than overwrite existing events;
- create a missing file;
- open it for writing only; and
- close the descriptor on a later `exec()`.

### 2.5 `append_fmt()`

This is a bounds-checked formatting helper. It appends formatted text to a
fixed-size record buffer and advances the write cursor without crossing the
end of the buffer.

### 2.6 `append_json_string()`

This helper escapes a normal string as valid JSON, including quotes,
backslashes, newline, carriage return, tab, and non-printable bytes. It is used
for thread names, module names, symbol names, and event names.

### 2.7 `append_address_info()`

Given a function address, `dladdr()` records:

- the absolute address;
- the containing ELF image or shared library;
- the offset from the module base; and
- the dynamic symbol name when available.

Although ASLR changes an absolute address, `module + module_offset` remains a
useful cross-run code identifier. A symbol may be absent if the binary is
stripped, the function is not exported, or an internal C++ symbol is hidden.

### 2.8 `append_stack()`

This function serializes the creator stack returned by `backtrace()`. Each
frame contains its address, module, module-relative offset, and any available
symbol name. The stack identifies which librealsense source path created a
runtime thread and maps opaque instances to their source-level functions.

### 2.9 `trace_write_line()`

This helper ensures that the file is open, terminates the record with a
newline, and emits it with one `write()`. One write per JSONL event reduces
interleaving between concurrent writers.

### 2.10 `trace_emit_simple()`

This emits an event containing only an event name, timestamp, and TID. It is
used for `tracer_loaded` and `tracer_unloaded`.

## 3. Lifecycle Event Emitters

### 3.1 `trace_emit_pthread_create()`

This event records:

- create-call begin and return times;
- caller TID;
- the new thread's `pthread_t` value;
- original entry, module, and module offset;
- original argument address;
- return code and success status; and
- creator stack.

At this point the parent normally does not know the child's kernel TID. The
child obtains it after entering the trampoline. The create and start records
are joined by their common `pthread_value`.

### 3.2 `trace_emit_thread_start()`

The child trampoline records the child start time, kernel TID, parent TID,
`pthread_t`, original entry and argument, create time, and current thread name.

`thread_start` means that the child entered the userspace trampoline. It is
not identical to the first observed scheduling interval. The model therefore
separates:

- `created_ms`: the parent's `pthread_create()` call;
- `started_ms`: entry into the child trampoline; and
- `first_run_ms`: the first LiME-observed execution interval.

### 3.3 `trace_emit_thread_exit()`

This event records the exit time, TID, parent TID, `pthread_t`, original
entry, return value, lifetime, exit mechanism, and final thread name. The main
`exit_kind` values are `return` and `pthread_exit`.

## 4. `trace_start_trampoline()`

The original call:

```c
pthread_create(&thread, attr, original_start, original_arg);
```

becomes:

```c
pthread_create(&thread, attr, trace_start_trampoline, context);
```

The trampoline:

1. reads and frees `trace_start_context`;
2. stores the entry, parent TID, and start time in TLS;
3. emits `thread_start`;
4. calls `original_start(original_arg)`;
5. emits `thread_exit` when the original function returns; and
6. returns the original result.

It preserves the original argument and return value and adds only an outer
wrapper around the librealsense entry.

## 5. Shared-Library Load and Unload

### 5.1 `trace_constructor()`

The `__attribute__((constructor))` function runs after the dynamic loader
loads `libtrace_pthreads.so` and before `main()`. It resolves the real pthread
functions, opens the trace file, and emits `tracer_loaded`.

`tracer_loaded` is not model time zero. The explicit `process_start` marker is
the model's origin.

### 5.2 `trace_destructor()`

When the library unloads or the process exits normally, this function emits
`tracer_unloaded` and closes the output descriptor.

## 6. Interposed pthread APIs

### 6.1 `pthread_create()`

The wrapper:

1. resolves the real function;
2. checks the recursion guard;
3. records time and caller TID;
4. captures the creator stack with `backtrace()`;
5. allocates `trace_start_context`;
6. replaces the original entry with `trace_start_trampoline`;
7. calls the real `pthread_create()`;
8. emits a create event; and
9. returns the real result.

If allocation fails, it calls the real API with the original entry so that
the application can continue, although the child may then lack complete
start/exit records.

### 6.2 `pthread_join()`

The wrapper emits `pthread_join_begin` before the real join and
`pthread_join_end` afterward. The records contain the waiter, target
`pthread_t`, wait duration, and return code.

The probe's `join_wait_ms` is not necessarily one `pthread_join()` duration,
because the probe also scans `/proc/self/task` while waiting for every extra
thread to disappear.

### 6.3 `pthread_detach()`

After calling the real detach API, the wrapper records the time, caller TID,
target `pthread_t`, and return code. This helps distinguish joined threads,
detached threads that exited, and detached threads with no observed exit.

### 6.4 `pthread_setname_np()`

After calling the real naming API, the wrapper records the caller, target
`pthread_t`, requested name, and result. Librealsense may name a thread after
it enters the trampoline, so a later `thread_name` event can supersede the
name observed at `thread_start`.

### 6.5 `pthread_exit()`

Before invoking the real non-returning function, the wrapper emits
`thread_exit` with `exit_kind = pthread_exit`. A normal return from an entry
function is emitted by the trampoline with `exit_kind = return`.

## 7. `trace_marker.h`

This header does not interpose any function. It provides the lightweight
explicit interface:

```c
rs_trace_phase_marker("some_phase");
```

The probe uses it to attach application meaning to the lifecycle timeline.

### 7.1 `rs_trace_boottime_ns()`

Like `trace_now_ns()`, this function reads `CLOCK_BOOTTIME`, placing phase
markers, pthread events, and LiME eBPF events in one clock domain. The code is
duplicated because `trace_pthreads.c` is an independent shared library while
the header is compiled directly into the C++ probe.

### 7.2 `rs_trace_gettid()`

This returns the kernel TID that emits the marker. The main probe thread emits
most current phase markers.

### 7.3 `rs_trace_marker_fd()`

This opens the same `RS_THREAD_TRACE_FILE` as the preload library. A function-
local static value caches the descriptor: `-2` means not yet opened, `-1`
means open failed, and a nonnegative value is a valid descriptor. Phase and
pthread records therefore append to one JSONL stream.

### 7.4 `rs_trace_append_json_string()`

This JSON-escapes a phase name. Current marker names use ASCII, so byte-wise
escaping is sufficient.

### 7.5 `rs_trace_phase_marker()`

This is the primary probe-facing interface. It writes a record such as:

```json
{
  "event": "phase_marker",
  "timestamp_ns": 123456789,
  "tid": 1234,
  "name": "cycle_01_after_pipeline_start"
}
```

Each call reads time and TID, formats a short record, and performs one write.
It is much lighter than the `backtrace()` work on a thread-creation path.

## 8. Probe Phase Markers

The probe's `cycle_marker()` and `mark_cycle()` combine a cycle number with a
phase. For example, `mark_cycle(1, "first_frame")` emits
`cycle_01_first_frame`.

| Marker | Meaning |
|---|---|
| `process_start` | Model time zero |
| `cycle_01_begin` | Start of a startup cycle |
| `before_context` | Before construction of `rs2::context` |
| `after_context` | Context construction complete |
| `before_pipeline_construction` | Before pipeline construction |
| `after_pipeline_construction` | Pipeline object constructed |
| `before_pipeline_start` | Before `pipeline.start()` |
| `after_pipeline_start` | `pipeline.start()` returned |
| `first_frame` | First frameset returned |
| `frames_complete` | Requested number of framesets received |
| `before_pipeline_stop` | Before `pipeline.stop()` |
| `after_pipeline_stop` | Stop returned |
| `before_object_destruction` | Before destroying librealsense objects |
| `after_object_destruction` | Object destruction complete |
| `threads_joined` | Every extra thread terminated |
| `thread_join_timeout` | Timed out waiting for thread termination |
| `cycle_01_end` | Current cycle complete |
| `process_exit` | Normal process exit |
| `process_error` | Exceptional process exit |

## 9. Data-Source Boundaries

`trace_pthreads.c` reports pthread lifecycle events, but it cannot tell whether
a live thread is running, ready, or sleeping. LiME supplies those scheduler
states.

`trace_marker.h` reports human-defined application boundaries, but it cannot
explain librealsense internal behavior by itself.

| Information | Source |
|---|---|
| Creation, entry, exit, join, and name | `trace_pthreads.c` |
| Context, pipeline, and frame phases | `trace_marker.h` |
| Running, ready, sleeping, and CPU | LiME |
| Temporal model and period detection | Offline Python analysis |

## 10. Measurement Overhead

The interposer is not free. The `pthread_create()` path performs
`backtrace()`, `dladdr()`, and JSON output in particular.

The instrumentation-overhead validation therefore compares:

1. tracing disabled;
2. pthread build/lifecycle instrumentation only; and
3. LiME plus the pthread interposer.

This experiment quantifies perturbation of startup time, thread-creation
latency, CPU use, memory use, inter-delivery time, and receive-path latency.
