# E3 — Kernel receive-path scheduling

[Main reproduction guide](../README.md) · [Pi setup](../setup/README.md) ·
[Previous: E2](../e2_uvc_pool/README.md) · [Next: E4](../e4_scheduling/README.md)

E3 measures freshness under deliberate localized scheduler starvation while
protecting first xHCI and then UVC copy workers. Its result is panel (b) of the
paper's host-path figure. This is a mechanism study, not E4's general workload.

For all four experiments together, use [Level 1 (`./reproduce.sh raw`)](../README.md#level-1-raw-data-to-paper-results)
or [Level 2 (`./reproduce.sh full`)](../README.md#level-2-complete-hardware-reproduction).
The commands below are the manual, experiment-specific alternative; do not run
them concurrently with the full-mode service.

## 1. Prerequisites

Complete setup/discovery and these prerequisite stages in the same campaign:

- [E1 `e1`](../e1_workers/README.md#2-capture): D455 stress profile at
  `runs/e1/profiles/d455_stress60.csv`.
- [E2 `e2-calibrate`](../e2_uvc_pool/README.md#2-capture): D435 stress profile at
  `runs/e2/profiles/stress.csv`.

Boot **non-RT URB16 with threadirqs**. Use the discovered IRQ mapping for
`single_d435` and `single_d455`. Both camera models use their 60-FPS stress
profile. E3 pins the selected IRQ thread and a 90%-duty-cycle RR-1 interferer
to CPU 3, with one OTHER background competitor; SDK workers use FIFO-RM.

The three treatments are xHCI/UVC-copy OTHER/OTHER, FIFO-90/OTHER and
FIFO-90/FIFO-89. Two models × three treatments × three runs = **18 accepted
full-path traces**. Effective policy is audited, including dynamically
identified UVC copy workers.

## 2. Capture

```sh
python3 reproduction/setup/discover.py --check
sudo -v
python3 reproduction/e3_kernel_path/run.py check e3
python3 reproduction/e3_kernel_path/run.py run e3
```

A successful stage writes `runs/completed/e3.json`; inspect `runs/e3.log` and
the per-model/treatment/repetition directories under `runs/e3/`. Retain all
attempts and their `SELECTED` markers so selection remains auditable.

## 3. Analyze and plot

```sh
.venv/bin/python reproduction/e3_kernel_path/run.py analyze \
  --input reproduction/runs --output reproduction/derived-e3-new
.venv/bin/python reproduction/e3_kernel_path/run.py analyze \
  --archive --input paperwriting/paper_used_raw_data \
  --output reproduction/derived-e3-paper
python3 reproduction/render.py reproduction/derived-e3-new
```

Each output directory must be new/empty. Analysis fails unless it finds 18
successful selected attempts, six cells and three repetitions per cell.

## 4. Inspect and compare

Under `OUTPUT/e3_kernel_path/`, inspect `e3_runs.csv`, `e3_summary.csv` and
`e3-kernel-path.tex` / `.pdf`. Non-fresh percentages pool counts over the three
runs; per-cell p99 is the median run p99 and maximum is the worst run maximum.
The plot uses the paper's blue/orange camera colors and logarithmic axis.
A measured zero is explicitly labelled at the axis floor.

| xHCI / UVC copy policy | Archived D435 non-fresh (%) | Archived D455 non-fresh (%) |
| --- | --- | --- |
| OTHER / OTHER | 68.95 | 20.36 |
| FIFO 90 / OTHER | 5.47 | 3.40 |
| FIFO 90 / FIFO 89 | 0.149 | 0.152 |

New starvation outcomes can differ. Compare treatment ordering, latency,
coverage and freshness together; a sequence gap makes a delivery non-fresh
even if no component was reused. `analyze.py` uses the existing per-run
latency summaries and freshness parser; `plot.py` draws the pooled CSV.
The archived latency summaries retain their historical upper-cutoff behavior;
a fresh corrected capture is not expected to reproduce that omission.
