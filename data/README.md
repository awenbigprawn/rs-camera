# RTNS paper: selected measurement metadata and figures

This directory contains the measurement metadata and Python code needed to
reconstruct the figures and tables in **From Threads to Fresh Frames:
Cross-Layer Modeling and Scheduling of RealSense on Linux**. It is published
on the [`data` branch](https://github.com/awenbigprawn/rs-camera/tree/data/data).

The metadata comes from the real, accepted paper experiments. Selection follows
the files actually read by the current analysis and figure/table builders, plus
the completion and selected-attempt markers those builders require. The package
contains **1,083 metadata files, 400,317,602 bytes uncompressed**, in two lossless
archive parts totaling **56,086,656 bytes (about 56 MB)**. Selected files retain
their original bytes and are checked against the original experiment archive's
hashes. [`metadata/FILES.json`](metadata/FILES.json) lists every retained file,
its packaged path, original campaign path (`original_path`), size, and SHA-256 hash.

Only metadata used by the paper is included. Full binary kernel/diagnostic
traces, unrelated tests, smoke campaigns, failed/redundant attempts, historical
plot inputs, and unused plotting scripts are excluded. This is a compact
figure/table reproduction package, not the complete experiment archive.

## Quick start

```sh
git clone --branch data --single-branch https://github.com/awenbigprawn/rs-camera.git
cd rs-camera
python3 data/reproduce.py
```

Use **Python 3.12 or newer**; validation used Python 3.12.3. The supported pipeline uses
only the standard library: no pip installation, cameras, Raspberry Pi, root
privileges, or submodules are required. Allow at least **1 GB free space** for
cloning, extraction, and outputs. You can also copy just `data/` elsewhere and
run its `reproduce.py`; all required local Python imports are included.

The entry point verifies the package, extracts the selected metadata to
`data/metadata-unpacked/`, verifies every extracted source file, and runs the
existing E1–E4 analyzers and the paper object builders. It then compares the
rebuilt values and E4 TikZ figures against the captured manuscript. A checksum,
missing measurement, incomplete population, or comparison failure stops the run.

Outputs are written to `data/generated/`:

- `derived/`: experiment CSVs, worker profiles, and a manifest recording the
  input paths and hashes consumed by the analysis.
- `paper/`: all **11 current paper figure/table objects** in LaTeX, the native E4
  TikZ figures, `freshness-patterns.json`, and the comparison report
  `validation.json`.

A verified extraction is reused after another checksum check. Output directories
must be new or empty; choose another directory for a second run:

```sh
python3 data/reproduce.py --verify-only
python3 data/reproduce.py --extract-only
python3 data/reproduce.py --output data/generated-review2
```

An incomplete extraction is preserved for inspection and is not silently reused.
Remove only that generated `metadata-unpacked/` directory before retrying.
`--input /path/to/extracted-metadata` accepts an existing copy of this exact
metadata selection, with its included `SHA256SUMS`.

### Generate CSV tables and PDF figures

To generate both analysis data and viewable PDFs, first install the TeX Live
packages that provide `pdflatex`, TikZ/PGFPlots, `standalone`, `booktabs`, `array`,
and Computer Modern fonts. On Ubuntu, install these dependencies once:

```sh
sudo apt-get update
sudo apt-get install texlive-latex-extra texlive-pictures texlive-fonts-recommended cm-super
```

Then run this single command from the repository root:

```sh
python3 data/reproduce.py --pdf --output data/generated-pdf
```

The command checks the archive parts, extracts `data/metadata/` into
`data/metadata-unpacked/` or reuses a verified extraction, runs the analyses,
and rebuilds the paper objects. It performs **46 comparisons** against the
captured manuscript and compiles **11 standalone figure/table PDFs**.
Python generates the analysis data and LaTeX/TikZ sources; `pdflatex` renders
the PDFs. No hardware capture or pip installation is needed.

| Output | Location |
| --- | --- |
| Analysis CSVs, worker profiles, and input provenance | `data/generated-pdf/derived/` |
| Figure/table LaTeX sources and 11 individual PDFs | `data/generated-pdf/paper/` |
| Numeric and TikZ comparison report | `data/generated-pdf/paper/validation.json` |
| Per-object PDF compilation logs | `data/generated-pdf/paper/*.render.log` |

The main result PDFs are `tab-steady-model-validation.pdf` (E1),
`fig-linux-host-path-results.pdf` (E2/E3), `fig-main-matrix.pdf` (E4 p99), and
`fig-main-matrix-max.pdf` (E4 maximum), all under the `paper/` output directory.
The command produces separate PDFs; it does not create a merged review PDF.
Captions remain in the `.tex` sources. TeX/font versions may affect PDF bytes;
numeric and native TikZ comparisons are independent of TeX.

For CSVs and LaTeX sources only, omit `--pdf`; TeX Live is then unnecessary:

```sh
python3 data/reproduce.py --output data/generated-tables
```

Output directories must be new or empty. To repeat PDF generation, use another
name such as `--output data/generated-pdf-review2`. Existing verified metadata
is reused and checked again; the source measurements are not modified.

## Measurement directories

After extraction, `metadata-unpacked/results/` contains the following nine
directories. Names follow the paper's experiment IDs and supplementary material.
Startup and stream ablation support the worker model used by E1.

| Directory | Paper correspondence | Files | Original directory under `rs-camera/results/` |
| --- | --- | ---: | --- |
| `e1_startup_worker_map/` | Figure 2: worker map; startup evidence | 121 | `rpi5/startup_linux612_uvc16_20260811/` |
| `e1_stream_ablation/` | Figure 2: worker map; stream attribution | 14 | `rpi5/model_stream_ablation_20260812/` |
| `e1_worker_timing/` | E1, Table 2: worker families and timing | 60 | `rpi5/c1_full_receive_path_20260819/` |
| `e2_uvc_pool/` | E2, Figure 4; supplementary Table 3: UVC patterns | 28 | `rpi5/d1_h1_single_d435_20260819/` |
| `e2_incomplete_frame_diagnostics/` | E2: aligned incomplete-frame observations | 1 | `freshness_path_diagnostics/freshness_validation_other_10min_v1/` |
| `e3_kernel_path_protection/` | E3, Figure 4: progressive kernel-path protection | 55 | `rpi5/h1_replacement_targeted_starvation_20260819/` |
| `e4_scheduling_30fps/` | E4, Figure 5 and supplementary Figure 1: 30 FPS | 384 | `rpi5/p1_host_latency_representative_20260820/` |
| `e4_scheduling_60fps/` | E4, Figure 5 and supplementary Figure 1: 60 FPS | 384 | `rpi5/p1_host_latency_supplement_20260820/` |
| `supp_instrumentation_overhead/` | Supplementary Tables 1 and 2: instrumentation overhead and data age | 36 | `rpi5/diagnostic_overhead_current_20260817/` |

Only the campaign directories have been renamed. Measurement file contents,
run/attempt names, and any historical paths recorded inside measurement files
are preserved. `metadata/FILES.json` maps each packaged file to its original
location. The archive and checksum lists use the new directory names.
The separate upstream calibration campaign `rpi5/p1_cross_layer_v2_20260820/`
is not included in this figure/table metadata selection.

## Figure and table mapping

Stable LaTeX labels are used for filenames below. All listed `.tex` outputs
appear in `generated/paper/`.

| Paper object | Output basename | Source and generation |
| --- | --- | --- |
| Main Figure 1: Linux receive path | `fig-linux-usb-path` | Conceptual TikZ source captured from the manuscript; no sampled data |
| Main Figure 2: worker map | `fig-thread-temporal-model` | Manuscript TikZ source, supported by 20 startup runs, D435 stream ablations, and E1 calibration metadata |
| Main Figure 3: timing definitions | `fig-timing-notions` | Conceptual TikZ timing diagram; positions illustrate definitions, not measurements |
| Main Figure 4: E2/E3 controls | `fig-linux-host-path-results` | Accepted frame-event records; E2 freshness counts and E3 non-fresh percentages are recomputed |
| Main Figure 5: E4 p99 latency | `fig-main-matrix` | 192 accepted runs / 64 cells; recorded frame metadata and archived per-run latency summaries |
| Main Table 1: experiment populations | `tab-experiment-populations` | Manuscript experimental-design table; analyzers check the corresponding run counts |
| Main Table 2: E1 worker families | `tab-steady-model-validation` | 12 accepted runs: recorded lifecycles, activation intervals, execution samples, and worker summaries |
| Supplementary Table 1: instrumentation overhead | `tab-instrumentation-overhead` | 18 accepted resource summaries and frame-event files |
| Supplementary Table 2: data age | `tab-instrumentation-age-overhead` | Oldest frame component at each delivery, followed by median per-run statistics |
| Supplementary Table 3: UVC patterns | `tab-uvc-freshness-patterns` | Frame-number reuse, catch-up gaps, and all-stream skips reconstructed from six accepted URB5 runs |
| Supplementary Figure 1: E4 maximum latency | `fig-main-matrix-max` | The same 192 E4 runs and their observed per-run maxima |

The retained campaign groups are startup, stream ablation, E1 calibration, E2
receive-pool comparison, aligned failure correlations, E3 targeted starvation,
E4 representative/stress matrices, and the accepted instrumentation study.
These groups contain only the required metadata files, not whole campaign trees.

## Analysis semantics

Freshness counts and frame timestamp statistics are reconstructed from recorded
`frame_events.csv` files. E1 uses the saved per-activation, lifecycle, timing,
and worker-summary records. E3/E4 latency figures use the **archived per-run
latency summaries that supplied the paper**. The compact package does not decode
binary traces again, because those traces are deliberately not included.

The saved [latency audit](paper/latency-sample-audit.json) documents a historical
250 ms cutoff: three E3 samples were excluded. Restoring them changes the D435
unprotected maximum from 179.97 to 288.54 ms, while E3 cell median p99 values and
freshness counts remain unchanged. No E4 sample was rejected by that range check.
The full software on `main` has the corrected decoder; this package retains the
paper's archived summary inputs rather than silently replacing them.

Main Table 2 retains the paper's intermediate CSV rounding before final range
formatting: execution/interval values to three decimals and utilization to four
(five for dispatchers). Full precision remains in `derived/e1_workers/`.
Some last displayed digits differ from rounding full-precision values directly.
The comparison report checks all 16 worker rows, both instrumentation tables,
UVC patterns, E2/E3 plot values, and byte identity of both E4 TikZ figures.

## Code and provenance

- `code/reproduction/` and `code/tools/` are a small snapshot of the existing
  analysis modules and their local imports. The archive directory lookup in
  `code/reproduction/analyze.py` is adapted to the paper-oriented directory names;
  the analysis calculations are unchanged.
- `code/paper_outputs.py` assembles the paper's combined E2/E3 panel and formats
  its measurement tables using the reconstructed values.
- `paper/objects/` contains only the 11 active figure/table source blocks.
  The conceptual diagrams and experimental-design table are identified above.
- `paper/figures/` contains the two original E4 TikZ figures for comparison.
- `SOURCE_MANIFEST.json` records source commits and hashes for copied code and
  manuscript objects, including the original hash and adaptation note for the
  modified archive directory lookup. `metadata/FILES.json` records the
  measurement-file subset and its original paths.
- `metadata/ARCHIVE.json` records archive order, sizes, and hashes.
  `SHA256SUMS` covers the distributable package; the extracted metadata has its
  own checksum list. Generated outputs and extracted files are Git-ignored.

Full hardware acquisition and setup instructions remain on
[`main`](https://github.com/awenbigprawn/rs-camera/tree/main/reproduction).
