# Reproducing and evaluating the paper artifact

The artifact supports two independent reproduction levels. Both use the same
experiment analyzers and paper-style plot code; only the source of measurements
differs. Run from the **rs-camera repository root**.

| Level | Input | Root command | Requirements |
| --- | --- | --- | --- |
| [Level 1: raw data to paper results](#level-1-raw-data-to-paper-results) | Supplied archived experiment data | `./reproduce.sh raw` | Laptop, Python environment, raw package; no camera or sudo |
| [Level 2: complete hardware reproduction](#level-2-complete-hardware-reproduction) | Newly acquired measurements | `./reproduce.sh full` | Configured Raspberry Pi 5, cameras and all three installed kernels |

To check a prepared Pi first, use the [3-second smoke test](#quick-smoke-test).

## Level 1: raw data to paper results

Install Python 3.12 with venv support and initialize the pinned submodules as
described in [step 2](#2-verify-the-software-or-reconstruct-archived-results).
For `raw` and `full`, `reproduce.sh` checks `.venv` (or `VENV_DIR`) against the
Python runtime and build-tool locks. A valid environment is reused without
installation; a missing or mismatched environment is prepared using
`scripts/install_python.sh`. Installation failures stop the entry point.
Help, `plan`, `status`, `stop` and `resume` do not install or modify dependencies.
For PDF output, install `texlive-latex-extra texlive-pictures`, then run:

```sh
./reproduce.sh raw
# Explicit locations:
./reproduce.sh raw --input paperwriting/paper_used_raw_data \
  --output reproduction/derived-paper-review
```

The script checks `SHA256SUMS` when supplied, analyzes E1–E4 and all four
supplementary sections, and generates CSV, TeX and standalone PDFs. The default
output is `reproduction/derived-raw/`, containing `checksums.log`, `analysis.log`,
`pipeline.json` and `derived/{e1_workers,e2_uvc_pool,e3_kernel_path,e4_scheduling}`.
A checksum or analysis failure stops the pipeline and records failure; it does
not modify the input data or contact hardware. An input tree without a checksum
file is labelled accordingly; the analysis manifest still hashes consumed files.

If TeX is unavailable, `./reproduce.sh raw --no-pdf` generates all CSV and TeX
artifacts and explicitly records that PDF compilation was skipped. After
installing TeX, use `python3 reproduction/render.py reproduction/derived-raw/derived`.
Default PDF mode checks TeX dependencies before starting. Use a new/empty output
directory for each run; no prior results are overwritten.

This level rebuilds published results from the saved experiment package,
including its per-run trace summaries. It does not redo hardware acquisition
or silently recompute old summaries using the corrected latency filter.

## Level 2: complete hardware reproduction

First complete [Pi setup](setup/README.md) through kernel installation and camera
connection. This level starts at data acquisition; installing the OS, dependency
builds and the matched kernels remains the one-time setup step.

```sh
./reproduce.sh plan                 # Read-only: inspect both levels and phase order
./reproduce.sh full                 # Start the full campaign; requests sudo
./reproduce.sh status               # Progress and currently active stage
```

`full` discovers the connected cameras, freezes their role selection and the
source snapshot, prepares adapters, and installs the root systemd service
`rs-camera-reproduction.service`. It then collects **all stages**, including
startup, ablation, calibrations, diagnosis and overhead; switches kernels and
**reboots automatically** between compatible groups; analyzes the new captures;
and generates CSV/TeX/PDF. Disconnecting the SSH terminal does not stop it.
Use an Ethernet SSH connection: Wi-Fi is disabled during measurement.
Root service execution avoids an expiring sudo ticket and does not install a
passwordless sudo rule. Full mode requires repository/output paths without spaces.

The default campaign is `reproduction/runs/full/`. To choose another directory,
pass `--output PATH` to full and every subsequent status/resume/stop command.
`--no-pdf` is also supported. A supplied `--config PATH` seeds existing camera
role preferences; all identities are verified against live discovery. Kernel
IRQ numbers may change across boots: those mappings alone are refreshed, with
per-boot inventories and per-stage prepared manifests retained. Physical camera
roles are never silently reassigned.

```sh
journalctl -u rs-camera-reproduction.service -f
./reproduce.sh stop                 # Stop service, preserve data, restore saved boot settings
./reproduce.sh resume               # Retry only when no uncompleted acquisition was started
```

At completion or a handled failure, the service disables itself and restores
saved **next-boot** settings and the previously up Wi-Fi link. It does not force
an extra final reboot, so the current kernel remains the last booted kernel
until you reboot. Setup/kernel/runtime details and per-run restoration remain
owned by the existing tools.

The runner resumes automatically across its planned reboots and skips only
stages with valid completion markers. A failed boot stops instead of looping.
An interrupted capture without a completion marker is preserved and requires
a new campaign directory: legacy stages are not all transactionally resumable.
`resume` can retry a failed preflight or final analysis/PDF generation, but does
not erase failed attempts. Source changes are rejected against the snapshot.
Do not start a manual campaign while the service is running.

Inspect `pipeline.json`, `completed/`, stage logs, `environment/`,
`boot-inventories/`, `source-snapshot.tar.gz` and `derived/` to audit the result.
The state is marked complete only after every required stage and requested
output step succeeds. Low-level manual instructions follow for reviewers who
want to inspect or execute individual steps.

## 1. Prepare the Raspberry Pi

Follow the [step-by-step setup guide](setup/README.md) in order:

1. [Install the OS and connect over Ethernet](setup/README.md#1-install-the-os-and-connect-over-ethernet).
2. [Obtain the artifact and its dependencies](setup/README.md#2-obtain-the-artifact-and-dependencies).
3. [Build the SDK, benchmarks and tracing tools](setup/README.md#3-install-and-build-the-tools).
4. [Install the three matched kernels](setup/README.md#4-build-and-install-the-matched-kernels).
5. [Connect and automatically discover cameras](setup/README.md#5-connect-and-discover-the-cameras).
6. [Check the platform and prepare the campaign](setup/README.md#6-check-and-prepare-the-campaign).

The complete experiment needs a Raspberry Pi 5, **two D435 cameras on different
xHCI controllers**, and **one D455 or D455F**. Use powered USB3 hubs and a wired
connection. The setup guide specifies the software versions and explains the
three kernel builds and four boot modes.

After the build, camera configuration requires no hand-entered serials or IRQs:

```sh
python3 reproduction/setup/discover.py
```

This writes `reproduction/setup/config.json` for all capture stages and
`reproduction/setup/inventory.json` for inspection: SDK identity, firmware,
USB port/speed/controller, IRQ mapping and supported stream profiles. It does
not start a stream or reset a camera. Repeated discovery preserves selected
roles and backs up changed configurations. Missing cameras, USB2 links, or an
invalid D435 topology prevent replacement of a working configuration.

## 2. Verify the software or reconstruct archived results

```sh
python3 -m unittest discover -s reproduction/tests -v
.venv/bin/python reproduction/analyze.py \
  --archive --input paperwriting/paper_used_raw_data \
  --output reproduction/derived-paper
```

Archive analysis needs the supplied raw-data package. Analysis and plot helpers
are tracked in this repository,
but no camera or sudo. Verify the raw package first using the instructions in
the README inside the separately supplied raw-data package. It reconstructs
E1–E4 and the startup, stream-ablation, diagnostic and overhead supplements.
Every analysis output directory must be new or empty. Outputs are separated
by experiment and accompanied by a `manifest.json` recording input hashes.

For archive-only evaluation on an Ubuntu laptop, install `python3-venv`, create
`.venv`, and install the supplied Benchkit dependency and NumPy:

```sh
sh scripts/install_python.sh
.venv/bin/python scripts/check_dependencies.py --sources --python --build-tools
```

## 3. Run the four experiments

Each linked guide gives prerequisites, exact capture commands, expected run
counts, analysis commands and the result to compare with the paper.

| Experiment guide | Question / paper output | Formal capture population |
| --- | --- | --- |
| [E1 — Worker structure and calibration](e1_workers/README.md) | Family timing table; startup and stream attribution | 12 calibration traces; 20 startup and 6 ablation runs |
| [E2 — UVC receive-pool comparison](e2_uvc_pool/README.md) | Host-path figure panel (a) | 12 comparison runs; 2 calibration traces |
| [E3 — Kernel receive-path scheduling](e3_kernel_path/README.md) | Host-path figure panel (b) | 18 runs: 2 models × 3 treatments × 3 repetitions |
| [E4 — Coordinated scheduling matrix](e4_scheduling/README.md) | Main latency matrix and supplementary maxima | 192 runs: 2 workloads × 4 interference conditions × 4 policies × 2 kernels × 3 repetitions |

Use one shared capture directory (`reproduction/runs` by default). The order
below satisfies calibration dependencies while grouping compatible kernels:

| Boot mode | Stages to execute in order | Commands and acceptance criteria |
| --- | --- | --- |
| RT URB16 | `e1`, `e2-calibrate` | [E1](e1_workers/README.md#2-capture), [E2](e2_uvc_pool/README.md#2-capture) |
| Non-RT URB16, `threadirqs` | `ablation`, `e3`, `e4-calibrate`, `e4-standard-threaded` | [E1](e1_workers/README.md#2-capture), [E3](e3_kernel_path/README.md#2-capture), [E4](e4_scheduling/README.md#2-capture) |
| Non-RT URB5, no `threadirqs` | `e2-urb5` | [E2](e2_uvc_pool/README.md#2-capture) |
| Non-RT URB16, no `threadirqs` | `startup`, `e2-urb16`, `e4-standard-hardirq`; optionally `diagnosis`, `overhead` | [E1](e1_workers/README.md#2-capture), [E2](e2_uvc_pool/README.md#2-capture), [E4](e4_scheduling/README.md#2-capture) |
| RT URB16 | `e4-rt-threaded` | [E4](e4_scheduling/README.md#2-capture) |

For manual execution, [kernel switching and post-reboot checks](setup/README.md#7-switch-kernels-between-stages)
are explicit; individual capture scripts do not switch kernels or reboot. The
root `full` controller performs these switches automatically. After each
reboot verify camera mapping with `discover.py --check`. Capture preflight
checks the kernel, compiler and calibrations. A successful stage writes
`runs/completed/STAGE.json`; detailed output is in `runs/STAGE.log`.

## 4. Analyze and inspect the figures

After completing the four principal experiments:

```sh
.venv/bin/python reproduction/analyze.py --input reproduction/runs \
  --output reproduction/derived-new --sections e1 e2 e3 e4
python3 reproduction/render.py reproduction/derived-new
```

The render command needs `pdflatex`, PGFPlots, standalone and booktabs (Ubuntu:
`texlive-latex-extra texlive-pictures`). Analysis itself emits CSV and TeX
without requiring TeX. Per-experiment guides list filenames and subset commands.
`reproduce.py analyze` also analyzes the supplements and requires those stages
to be completed. Neither command modifies the manuscript or source captures.

Fresh measurements should reproduce the design and statistical definitions,
not identical numbers. Compare new and archive CSVs alongside the same styled
figures. Existing archive latency summaries retain the historical 250-ms
cutoff; current capture analysis keeps larger nonnegative latencies. Exporting
an archive does not silently reanalyze its raw traces.

## 5. Retain evidence for artifact evaluation

Keep the camera config/inventory, `runs/prepared.json`, `runs/environment/`,
per-attempt traces, selection markers, completion markers, analysis manifest,
CSVs and TeX/PDF outputs. Do not discard failed attempts when reporting retry
behavior. E1/E2/E3/E4 use their respective acceptance checks; incomplete E3/E4
populations cause analysis to fail rather than generate a partial paper figure.

Freeze the exact source used for a campaign:

```sh
python3 reproduction/bundle_sources.py --output /tmp/rs-camera-source.tar.gz
```

The source snapshot records dependency commits and tracked dependency patches;
it does not include raw data, vendor trees, kernels or built executables.
See [artifact inputs and provenance](setup/README.md#2-obtain-the-artifact-and-dependencies)
and the [detailed capture protocol](workflow.rst). No repository publication or
artifact certification is implied by local test results.

Validation so far: the root `raw --no-pdf` command verified the archived package
and reproduced all eight analysis sections; 11 CSVs match the previous workflow
byte for byte. State-machine tests simulate all kernel phases, failed boots and
interrupted acquisitions; the generated systemd unit passes syntax validation. Automatic discovery was also tested on the prepared Pi with two
D435 and two D455F cameras. This does not constitute a complete fresh-install
or full four-experiment hardware validation.

## Analysis-code provenance

The shared functions in `paper_metrics.py` are the functions used by the paper's
`prepare_figure_data.py`, retained without changing their metric definitions.
E4's `metrics.py` preserves `parse_run` and `reconstruct_freshness` from
`prepare_main_matrix_host_latency.py`; `tikz.py` preserves the paper's native
renderer. Their unrelated manuscript paths and command-line batch jobs are
omitted. The independent manuscript checkout is not a runtime dependency.

See [dependency locks](../dependencies/README.md) for fixed source commits,
Python libraries, Rust toolchain and the Pi system package profile.

## Quick smoke test

On a prepared Pi, check all main-experiment configurations using one repetition
and **3-second measurement windows**:

```sh
./reproduce.sh full --smoke --no-pdf
./reproduce.sh status --output reproduction/runs/smoke
```

The calibration windows remain 30 seconds, once per workload: 3-second traces
cannot estimate workers with 5-second timer periods. This is eight calibration
windows (four minutes total), followed by 4 E2, 6 E3 and 64 E4 short runs. Camera
resets, warm-up, parsing and kernel reboots take additional time. Startup,
stream-ablation, overhead and the 600-second diagnostic supplements are omitted.
The normal full-reproduction protocol is unchanged.

The smoke manifest records its protocol and generated figures are visibly
labelled. Short traces validate execution and parsing, not performance claims.
Use `python3 reproduction/analyze.py --input reproduction/runs/smoke --output
reproduction/derived-smoke --smoke --sections e1 e2 e3 e4` for manual analysis;
compile its figures with `python3 reproduction/render.py reproduction/derived-smoke`.
`--calibration-seconds 3` explicitly tests the too-short calibration path; it may
stop with insufficient observations and does not silently fabricate a profile.

### Hardware verification, 2026-10-04

The smoke protocol was exercised on a Pi 5 with two D435 cameras and a D455F,
using the pinned dependencies and all three required kernels. All 74 measurement
captures succeeded: 4 E2, 6 E3 and 64 E4 configurations, once each. Recorded
measurement windows ranged from 3000.057 to 3001.685 ms. All eight 30-second
calibration captures also succeeded. E4 latency matching covered at least 98.89%
of deliveries, above the unchanged 95% acceptance threshold.

The first automatic export exposed E4's hard-coded three-repetition plot check.
After fixing it, the same captured data passed the corrected pipeline analysis
entry point on the laptop and produced all five PDFs; no acquisition was repeated.
The original failed export log was retained. A backup verified 5,823 files with
no differences, and the Pi's saved next-boot settings were restored. The service
was disabled after the run. This is a functional check, not a completed formal
30-second, three-repetition paper replication; supplements were not exercised.

Regression checks passed 28 tests. Re-analysis of the archived paper data
regenerated 17 CSV and five TeX files byte-for-byte identically to the prior
exports, preserving the normal protocol and figure style.
