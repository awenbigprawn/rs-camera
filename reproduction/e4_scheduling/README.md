# E4 — Coordinated scheduling matrix

[Main reproduction guide](../README.md) · [Pi setup](../setup/README.md) ·
[Previous: E3](../e3_kernel_path/README.md)

E4 compares userspace policies coordinated with the kernel receive path.
It generates the paper's main p99 host-latency matrix and supplementary
maximum-latency matrix. Run commands from the repository root.

For all four experiments together, use [Level 1 (`./reproduce.sh raw`)](../README.md#level-1-raw-data-to-paper-results)
or [Level 2 (`./reproduce.sh full`)](../README.md#level-2-complete-hardware-reproduction).
The commands below are the manual, experiment-specific alternative; do not run
them concurrently with the full-mode service.

## 1. Prerequisites

Complete setup/discovery and prepare one shared campaign. The selected
`d435_pair` must use different xHCI controllers. E4 needs **its own** calibration;
E1/E2 profiles do not replace it. GPU cells require the built ncnn/Vulkan noise
binary, supplied model definition and working Pi V3D driver.

| Stage | Boot mode | Expected population |
| --- | --- | --- |
| `e4-calibrate` | Non-RT URB16 + threadirqs | 2 workloads × 3 = 6 traces |
| `e4-standard-threaded` | Non-RT URB16 + threadirqs | RR-RM/FIFO-RM/Deadline × 2 workloads × 4 noise conditions × 3 = 72 |
| `e4-standard-hardirq` | Non-RT URB16 without threadirqs | OTHER × 2 workloads × 4 noise conditions × 3 = 24 |
| `e4-rt-threaded` | RT URB16 | 4 policies × 2 workloads × 4 noise conditions × 3 = 96 |
| `overhead` (supplement) | Non-RT URB16 without threadirqs | 2 workloads × off/build-only/full × 3 = 18 |

The principal matrix contains **192 runs in 64 cells**. Each cell has three
independent 30-second measurement windows. Calibration, warm-up, resets,
tracing analysis and retries add time beyond those windows.

## 2. Capture

On **non-RT URB16 with threadirqs**:

```sh
sudo -v
python3 reproduction/e4_scheduling/run.py check e4-calibrate
python3 reproduction/e4_scheduling/run.py run e4-calibrate
python3 reproduction/e4_scheduling/run.py check e4-standard-threaded
python3 reproduction/e4_scheduling/run.py run e4-standard-threaded
```

On **non-RT URB16 without threadirqs**:

```sh
sudo -v
python3 reproduction/e4_scheduling/run.py check e4-standard-hardirq
python3 reproduction/e4_scheduling/run.py run e4-standard-hardirq
# Optional instrumentation supplement:
python3 reproduction/e4_scheduling/run.py check overhead
python3 reproduction/e4_scheduling/run.py run overhead
```

On **RT URB16**:

```sh
sudo -v
python3 reproduction/e4_scheduling/run.py check e4-rt-threaded
python3 reproduction/e4_scheduling/run.py run e4-rt-threaded
```

[Switch and reboot between modes](../setup/README.md#7-switch-kernels-between-stages).
Check the corresponding completion markers and logs. Profiles are under
`runs/e4-profiles/`; results are split by workload under
`runs/e4-representative/` and `runs/e4-stress/`. Preserve per-cell `DONE` markers,
policy audits and workload validation artifacts.

## 3. Analyze and plot

```sh
.venv/bin/python reproduction/e4_scheduling/run.py analyze \
  --input reproduction/runs --output reproduction/derived-e4-new
.venv/bin/python reproduction/e4_scheduling/run.py analyze \
  --archive --input paperwriting/paper_used_raw_data \
  --output reproduction/derived-e4-paper
python3 reproduction/render.py reproduction/derived-e4-new
```

Use new/empty outputs. Add `--with-supplements` once overhead acquisition is
complete. The analyzer verifies success, phase, policy, interference, topology
and coverage via the existing parser, then requires exactly 192 accepted runs,
64 cells and three repetitions each. A missing phase is an analysis failure.

## 4. Inspect and compare

Under `OUTPUT/e4_scheduling/`:

- `main_matrix_host_latency_runs.csv`: 192 per-run rows.
- `main_matrix_host_latency_summary.csv`: 64 aggregated cells.
- `e4-p99.tex` / `.pdf`: bars are the mean of three per-run p99 values; dots
  retain individual runs; dashed lines indicate nominal frame periods.
- `e4-max.tex` / `.pdf`: the maximum observed latency across the three runs.
- With supplements: `overhead_runs.csv` and `overhead_summary.csv`.

Red NF labels count deliveries with reuse or a sequence gap, once per delivery.
The archive has 159 non-fresh deliveries across non-RT and one across RT;
these counts are comparison evidence, not mandatory counts for a fresh run.
Evaluate latency and freshness together, including zero-timeout behavior and
coverage. Preserve anomalous overhead runs in the analysis.

`analyze.py` reuses the existing host-latency parser; `plot.py` calls the
paper's original TikZ renderer for identical E4 styling. Neither silently
recomputes old raw traces or modifies manuscript figures.
