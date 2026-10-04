Detailed capture protocol (preserved from the original workflow)
===============================================================

See README.md for the two reproduction levels and the four-experiment layout.
This document describes manual reproduce.py stages. The root reproduce.sh full
controller additionally handles systemd scheduling, kernel switches and reboots. Analysis output now has
one subdirectory per experiment, and plotting is automatic for E1--E4.

RTNS 2026 reproduction entry points
==================================

This directory organizes the existing experiment code. It supports the
already prepared Raspberry Pi 5 and rs-camera checkout used by the paper;
it is not a fresh-OS installer. Offline reconstruction, adapter syntax, and
the latency-filter regression have been checked locally. A four-run hardware
smoke test passed on the Pi on 2026-10-04; see Hardware validation below.

Files
-----

* reproduce.py: inspect the experiment plan, generate portable adapters,
  check prerequisites, and run one kernel-compatible stage.
* analyze.py: reconstruct startup, stream-ablation, E1, E2, E3, E4,
  instrumentation-overhead, and diagnostic data in a separate output directory.
* bundle_sources.py: preserve the actual experiment sources, including
  untracked files; record dependency commits and tracked dependency patches.
* setup/config.example.json: camera inventory (SDK serial/model/IRQ) and experiment selections.
* validation/archived-results/: local reconstruction of the original raw-data
  package. These are archive results, not a new hardware experiment.

Hardware and software prerequisites
-----------------------------------

Use two D435 cameras on separate Pi 5 xHCI controllers, one D455, and the
powered SuperSpeed topology described in the paper. The selection section of
config.json chooses the D435 pair, the single D435
and D455, and the startup/stream-ablation D435. All physical identifiers live
in the cameras section. Use SDK serials, not the different USB sysfs serials.
Check E3's selected camera/controller mapping in /proc/interrupts; IRQ
numbers and usbN labels must be configured for the actual machine.

The existing checkout must contain GCC/G++ 13.3.0, the RelWithDebInfo
V4L2 benchmark and instrumented librealsense 2.58.3 build, Python environment,
LiME, trace-cmd, the UVC eBPF monitor build prerequisites, GPU workload assets,
and the matched 6.12.96 BTF kernel images. See tools/realsense_steady_bench/README.md,
tools/realsense_startup_bench/README.md and tools/rpi_kernel_patch/README.md
in rs-camera for their existing setup procedures. The SDK diagnostic patches
are required in addition to the upstream SDK version.

Kernel names are checked because the recorder's dynamic probes depend on the
paper's kernel layout. UVC_URBS is a build-time setting; a renamed kernel does
not establish that its UVC request pool matches the experiment. Keep the
installed images/modules and their build provenance with the artifact.
The preflight also checks the compiler version and benchmark build mode;
the run records compiler output, generated flags.make files, code hashes,
USB topology, and the kernel command line. These checks do not replace
preparing the binaries and validating the camera topology on the target.

Use wired/local access and disable Wi-Fi before acquisition. The new entry
point does not stop networking, edit boot files, install services, or reboot.
The invoked benchmark code does change scheduling policies, camera state,
USB autosuspend, CPU frequency and caches as required by the experiment.
Do not run another camera experiment concurrently.

Prepare
-------

From the rs-camera repository root::

    python3 reproduction/setup/discover.py
    # Review the automatically saved setup/config.json and setup/inventory.json.
    python3 reproduction/reproduce.py plan
    python3 reproduction/reproduce.py fix-latency
    python3 reproduction/reproduce.py prepare

The default output is reproduction/runs. Use --output PATH
before the action to choose a different directory, consistently for all
stages and post-processing. Existing results are not replaced. Generated
shell adapters are in OUTPUT/entrypoints and can be inspected before use.
Source-template changes cause adapter generation to fail if an expected
boundary is no longer present; changes after preparation are checked again
before execution. Run prepare after changing experiment code, using a fresh
output directory for a new configuration/campaign.

fix-latency makes only one source edit: it removes the 250-ms upper cutoff
from analyze_full_receive_path.py, retaining the negative-latency rejection.
It saves the original source under OUTPUT/source-before-fix. It does not
change matching tolerances, replay old traces, or overwrite archived numbers.
The original archive does contain three E3 latencies omitted by this cutoff.
Fresh captures analyzed with the corrected script retain those values.

Capture stages
--------------

Run the following actions individually after booting the indicated kernel.
Prefix each with ``python3 reproduction/reproduce.py run``.
``check STAGE`` performs preflight without launching an experiment.
Run ``sudo -v`` first; the original tools require noninteractive sudo for the
whole stage. Do not set up permanent passwordless access just for this wrapper.

1. RT URB16, 6.12.96-rpi5-rt-btf-uvc16+:
   e1 (12 traces), then e2-calibrate (2 traces).
2. Non-RT URB16, 6.12.96-rpi5-standard-btf-uvc16+, with threadirqs:
   ablation (6), e3 (18), e4-calibrate (6), e4-standard-threaded (72).
3. Non-RT URB5, 6.12.96-rpi5-standard-btf+, without threadirqs:
   e2-urb5 (6).
4. Non-RT URB16, 6.12.96-rpi5-standard-btf-uvc16+, without threadirqs:
   startup (20), e2-urb16 (6), e4-standard-hardirq (24), overhead (18),
   diagnosis (one 600-s trace).
5. RT URB16:
   e4-rt-threaded (96).

Example::

    sudo -v
    python3 reproduction/reproduce.py check e1
    python3 reproduction/reproduce.py run e1

The E1 step generates per-camera/workload profiles automatically. E3 uses
the separately calibrated single-D435 profile and the full-path D455 profile,
as the original campaign did. E4 uses its own six calibration traces. Do not
substitute E1 timing parameters for E4's profile source.

E4 contains 192 runs total, split across three kernel/IRQ phases. Startup,
ablation, calibrations and overhead are additional runs. Log output goes to
OUTPUT/STAGE.log. Completion markers are written only after commands succeed.
The original per-attempt logs and selection markers are retained. Some legacy
stages can resume completed cells, but interrupted startup/overhead/calibration
stages are not transactionally resumable; retain the partial output and use a
new campaign directory instead of silently combining trials.

Analyze fresh captures
----------------------

After all stages complete, use the same entry point::

    python3 reproduction/reproduce.py analyze

For an explicitly named output directory::

    python3 reproduction/analyze.py \
      --input reproduction/runs \
      --output reproduction/derived-new

Use --sections e1 e2 (etc.) to analyze completed subsets. The output directory
must be empty. E1 exports signatures, creator-stack-derived roles, execution
maxima, minimum logical intervals, and observed utilization. E2/E3 freshness
is reconstructed from all frame events. E4 exports 192 per-run rows, 64 cells,
and both p99 and maximum TikZ figures. Overhead retains anomaly counts rather
than dropping new anomalous trials automatically. Startup uses the original
model builder. Diagnosis exports all correlations, including a legitimate
zero-event result; the two originally observed short-frame events are not a
required outcome of a rerun.

The exported E1 utilization is median per-run CPU residency divided by the
measured window, summed over equivalent instances. This reproduces the table's
observed utilization; it is not maximum execution divided by minimum interval.
Execution maxima and intervals reuse the current logical-job reconstruction.
Keep full-precision outputs. The archive check reproduces the principal-family
execution maxima in the paper CSV; interval and utilization differences are
within the rounding precision of that CSV.

The exporter consumes the per-run latency summaries produced during capture.
It deliberately does not silently recompute existing raw traces. In particular,
exporting an old archive after fix-latency still reports that archive's original
latency summaries. It labels this in manifest.json. Use a separate raw-trace
reanalysis and review the differences before replacing published data.

Fresh experiments reproduce the design and statistics, not identical numbers.
Thermal conditions, scheduling and intermittent USB events can change counts.
Figures and tables written directly in the manuscript are not automatically
replaced; review the exported values before updating the paper.

Rebuild the archived paper data
------------------------------

From the existing data bundle::

    python3 reproduction/analyze.py \
      --archive --input paperwriting/paper_used_raw_data \
      --output reproduction/derived-archive

Alternatively use --archive --input results with the original results tree.
The default covers all eight analysis sections listed above. This path does
not need cameras or root. It retains the archived E3 filtering behavior in
existing summaries; validation of the old figure and correction of the old
latency distribution are separate operations.

Hardware validation
-------------------

On 2026-10-04, the configured Pi completed four 30-second acquisitions on
6.12.96-rpi5-standard-btf-uvc16+ with threadirqs: one dual-D435 calibration,
dual-D435 FIFO-RM with four CPU competitors, dual-D435 Deadline without
interference, and single-D455F full receive-path tracing. Both calibration
and full-path traces produced scheduling profiles. The FIFO and Deadline
cells passed the existing policy, workload and UVC-monitor validation.
All runs had zero measurement timeouts; observed frame reuse and sequence
gaps were retained. Latency matching coverage was 99.83%, 99.94% and 100%
for the three latency runs, respectively.

All four attached cameras enumerated at USB3 speed; acquisition exercised
two D435 cameras and one D455F. CPU frequency, USB power settings and xHCI
priorities were restored. This checks representative acquisition and analysis
paths, not the complete paper matrix or the RT/URB5 kernel variants.
Local evidence is in validation/pi-smoke-20261004/smoke-evidence.tar.gz;
raw traces remain in the Pi's reproduction-smoke-20261004/runs.

Freeze source and run checks
---------------------------

The required historical campaign scripts, analyzers and paper plotting helpers
are now tracked in this repository. Create an additional source bundle to record
the working tree used by a particular campaign::

    python3 reproduction/bundle_sources.py \
      --output reproduction/source-snapshot.tar.gz
    python3 -m unittest discover -s reproduction/tests -v

The bundle includes current experiment code and the new wrappers, but not
vendor dependency trees, binaries, kernel images, GPU model assets or results.
Dependency commits and tracked diffs are recorded; inspect any untracked vendor
files listed in SOURCE_MANIFEST.json before claiming a self-contained release.
No Git commit, push, Pi reboot, or hardware campaign is performed by preparation
or offline analysis.
