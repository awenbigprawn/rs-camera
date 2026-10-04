# E2 — UVC receive-pool comparison

[Main reproduction guide](../README.md) · [Pi setup](../setup/README.md) ·
[Previous: E1](../e1_workers/README.md) · [Next: E3](../e3_kernel_path/README.md)

E2 compares compile-time UVC request pools of 5 and 16 URBs for one D435,
with FIFO-RM SDK workers and four OTHER CPU competitors. Its principal output
is panel (a) of the paper's host-path figure. Commands run from repo root.

For all four experiments together, use [Level 1 (`./reproduce.sh raw`)](../README.md#level-1-raw-data-to-paper-results)
or [Level 2 (`./reproduce.sh full`)](../README.md#level-2-complete-hardware-reproduction).
The commands below are the manual, experiment-specific alternative; do not run
them concurrently with the full-mode service.

## 1. Prerequisites

Complete setup, discovery and `reproduce.py prepare`. The `single_d435` role
selects the camera. Calibration under RT URB16 supplies the same single-D435
profiles to E2 and E3; do not substitute E1's model-level calibration.

| Stage | Boot mode | Expected population |
| --- | --- | --- |
| `e2-calibrate` | RT URB16 | 1 representative + 1 stress calibration |
| `e2-urb5` | Non-RT URB5 without `threadirqs` | 2 workloads × 3 = 6 comparison runs |
| `e2-urb16` | Non-RT URB16 without `threadirqs` | 2 workloads × 3 = 6 comparison runs |
| `diagnosis` (supplement) | Non-RT URB16 without `threadirqs` | One 600-second path trace |

URB count is a build-time driver setting; renaming a kernel does not change it.

## 2. Capture

On **RT URB16**, after E1 calibration:

```sh
sudo -v
python3 reproduction/e2_uvc_pool/run.py check e2-calibrate
python3 reproduction/e2_uvc_pool/run.py run e2-calibrate
```

On **non-RT URB5 without threadirqs**:

```sh
sudo -v
python3 reproduction/e2_uvc_pool/run.py check e2-urb5
python3 reproduction/e2_uvc_pool/run.py run e2-urb5
```

On **non-RT URB16 without threadirqs**:

```sh
sudo -v
python3 reproduction/e2_uvc_pool/run.py check e2-urb16
python3 reproduction/e2_uvc_pool/run.py run e2-urb16
# Optional additional diagnostic acquisition:
python3 reproduction/e2_uvc_pool/run.py check diagnosis
python3 reproduction/e2_uvc_pool/run.py run diagnosis
```

[Switch/reboot explicitly between modes](../setup/README.md#7-switch-kernels-between-stages).
Check completion markers and logs under `runs/`. Profiles are in
`runs/e2/profiles/`; formal runs are in `runs/e2/formal/`.

## 3. Analyze and plot

```sh
.venv/bin/python reproduction/e2_uvc_pool/run.py analyze \
  --input reproduction/runs --output reproduction/derived-e2-new
.venv/bin/python reproduction/e2_uvc_pool/run.py analyze \
  --archive --input paperwriting/paper_used_raw_data \
  --output reproduction/derived-e2-paper
python3 reproduction/render.py reproduction/derived-e2-new
```

Use new/empty output directories. Add `--with-supplements` to analyze diagnosis
as well; its correlation files must exist. A valid trace with zero correlated
anomalies is accepted: the two anomalies discussed in the paper are not a
required count for a fresh stochastic diagnostic run.

## 4. Inspect and compare

`OUTPUT/e2_uvc_pool/uvc_pool.csv` contains four pool/workload rows. The analyzer
requires three runs per row and checks delivery counts against frame events.
`e2-uvc-pool.tex` / `.pdf` plots partially stale framesets and deliveries with
a stream sequence gap using the paper's gray/blue pool colors.

Archived reference counts, pooled across three runs:

| Pool | 30 FPS stale | 30 FPS gap-bearing | 60 FPS stale | 60 FPS gap-bearing |
| --- | --- | --- | --- | --- |
| URB5 | 2 | 2 | 10 | 13 |
| URB16 | 0 | 0 | 0 | 0 |

Categories can overlap and must not simply be summed as distinct deliveries.
The raw CSV also retains component-level duplicate/sequence-gap counts.
Fresh counts need not equal this archive. Preserve zero counts and retries
instead of selecting only runs that match the published numbers.

`analyze.py` reuses the paper's freshness parser; `plot.py` generates panel (a)
from the resulting CSV. Diagnostic exports are separate from the comparison.
