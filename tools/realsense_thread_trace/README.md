# RealSense Thread Lifecycle Trace

Milestone 1 implements a real pthread lifecycle trace for a minimal
`librealsense` pipeline application.

## Build debug librealsense and probe

```bash
tools/realsense_thread_trace/build_librs_debug.sh
```

The build uses `RelWithDebInfo`, exports `compile_commands.json`, and adds
`-fno-omit-frame-pointer` to C and C++ debug builds.

## Run one trace

```bash
python3 tools/realsense_thread_trace/run_trace.py \
  --output tools/realsense_thread_trace/output \
  -- build-realsense-thread-trace/realsense_thread_lifecycle_probe
```

Optional sampling mode:

```bash
python3 tools/realsense_thread_trace/run_trace.py \
  --output tools/realsense_thread_trace/output_perf \
  --perf \
  --perf-attach-delay 3 \
  -- build-realsense-thread-trace/realsense_thread_lifecycle_probe
```

Perf sampling is observational. `thread_function_summary.csv` reports sampled
function activity, not a complete function-call trace.

Useful probe options:

```bash
build-realsense-thread-trace/realsense_thread_lifecycle_probe \
  --frames 300 \
  --steady-state-after 30 \
  --serial SERIAL
```

## Current outputs

- `thread_trace.jsonl`: raw JSONL events from `LD_PRELOAD` pthread interception
  and application phase markers.
- `ldd.txt`: dynamic loader view for the traced executable.
- `app_stdout.txt` and `app_stderr.txt`: captured application output.
- `thread_summary.csv`: basic parsed lifecycle rows.
- `symbolized_thread_trace.json`: raw events augmented with symbolized stack
  frames, creator-site inference, and best-effort child entry inference.
- `thread_timeline.svg`: horizontal tree timeline with phase markers and
  parent-to-child creation connectors.
- `thread_timeline.html`: scrollable HTML wrapper for the SVG with native SVG
  hover tooltips.
- `thread_timeline.png`: best-effort raster export when ImageMagick SVG support
  is available.
- `thread_function_summary.csv`: optional `perf` sample-derived per-thread
  function activity summary.

The trace covers only user-space pthreads inside the traced process. It does
not directly show kernel UVC worker threads, IRQ threads, firmware threads, or
external processes.

The preliminary source-side thread creation table is written at:

```text
THREAD_CREATION_STATIC_ANALYSIS.md
```
