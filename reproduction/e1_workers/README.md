# E1 — Worker structure and calibration

[Main reproduction guide](../README.md) · [Pi setup](../setup/README.md) ·
[Next: E2](../e2_uvc_pool/README.md)

E1 identifies recurring worker families and estimates execution, activation
interval and observed utilization. Its principal paper result is a **table**.
Startup and stream-subset runs support worker identification. All commands run
from the repository root after completing setup and automatic camera discovery.

For all four experiments together, use [Level 1 (`./reproduce.sh raw`)](../README.md#level-1-raw-data-to-paper-results)
or [Level 2 (`./reproduce.sh full`)](../README.md#level-2-complete-hardware-reproduction).
The commands below are the manual, experiment-specific alternative; do not run
them concurrently with the full-mode service.

## 1. Prerequisites

Use the shared `reproduction/runs` campaign prepared by `reproduce.py prepare`.
Roles come from `setup/config.json`: `single_d435`, `single_d455` and
`startup_d435`. E1 supplies the D455 profile consumed by E3; complete the `e1`
stage before E3. Keep the same camera selection throughout the campaign.

| Stage | Boot mode | Expected population |
| --- | --- | --- |
| `e1` | RT URB16 | D435/D455 × representative/stress × 3 = 12 full-path traces |
| `ablation` | Non-RT URB16 + `threadirqs` | Depth / Depth+Color × 3 = 6 runs |
| `startup` | Non-RT URB16 without `threadirqs` | 20 D435 lifecycle runs |

## 2. Capture

On **RT URB16**:

```sh
sudo -v
python3 reproduction/e1_workers/run.py check e1
python3 reproduction/e1_workers/run.py run e1
```

When the main schedule reaches **non-RT URB16 + threadirqs**:

```sh
sudo -v
python3 reproduction/e1_workers/run.py check ablation
python3 reproduction/e1_workers/run.py run ablation
```

When it reaches **non-RT URB16 without threadirqs**:

```sh
sudo -v
python3 reproduction/e1_workers/run.py check startup
python3 reproduction/e1_workers/run.py run startup
```

Use [the setup guide](../setup/README.md#7-switch-kernels-between-stages) to
switch and reboot. Successful stages leave `runs/completed/STAGE.json` and
`runs/STAGE.log`. Calibration profiles are generated automatically under
`runs/e1/profiles/`. The analyzer requires three accepted calibration attempts
per model/workload; startup analysis requires twenty recorded OTHER launches.

## 3. Analyze and plot

Principal result from fresh captures:

```sh
.venv/bin/python reproduction/e1_workers/run.py analyze \
  --input reproduction/runs --output reproduction/derived-e1-new
```

Add `--with-supplements` once startup and ablation are complete. Rebuild the
paper archive independently, including supplements:

```sh
.venv/bin/python reproduction/e1_workers/run.py analyze --with-supplements \
  --archive --input paperwriting/paper_used_raw_data \
  --output reproduction/derived-e1-paper
python3 reproduction/render.py reproduction/derived-e1-new
```

The output must be new/empty. The render step requires the TeX packages listed
in the main guide. It is optional for inspecting CSVs and generated TeX.

## 4. Inspect and compare

Under `OUTPUT/e1_workers/`:

- `e1_families.csv`: family signatures, instance counts, maximum logical-job
  execution, minimum observed interval and median run utilization.
- `e1_instances.csv`: per-instance execution/interval evidence.
- `profiles/`: scheduling profile CSVs and metadata.
- `e1-families.tex` / `.pdf`: the table in Representative/Stress panels.
- With supplements: `startup/` reconstructed model and `ablation_threads.csv`.

Compare rows by camera, workload and family. The archive reproduces the paper's
frame-driven capture/application families and slower timer families, including
the D455 thermal monitor; the paper reports the same startup topology in all
20 launches. Timing values can differ in a fresh run. Utilization is median
per-run CPU residency, summed across equivalent instances, rather than the
ratio of the execution maximum to interval minimum. Dispatcher observations
mainly characterize idle timeouts, not arbitrary event-driven workload bounds.

`analyze.py` reuses the startup model and deadline-profile generators;
`plot.py` formats the family CSV as the paper-style table. Keep signatures and
full-precision CSVs when explaining differences from rounded paper values.
