"""Repository paths and fixed controls shared by RealSense benchmarks."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
BENCHKIT_PATH = REPO_ROOT / "deps" / "benchkit"
DEFAULT_LIME = REPO_ROOT / "deps" / "lime-rtw" / "target" / "release" / "lime-rtw"
TRACER_SOURCE = REPO_ROOT / "tools" / "realsense_thread_trace" / "trace_pthreads.c"
CPUFREQ_BASE = Path("/sys/devices/system/cpu/cpufreq")
CPU_LOCK_SCRIPT = REPO_ROOT / "scripts" / "lock_cpu_freq.sh"
CPU_RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore_cpu_freq_default.sh"
RSUSB_HELPER = REPO_ROOT / "scripts" / "realsense_rsusb_uvc.sh"

PAPER_BACKEND = "v4l2"
PAPER_USB_KERNEL_DRIVER = "uvcvideo"
PAPER_CPU_FREQUENCY_MHZ = 1500
PAPER_DROP_CACHES_BEFORE_RUN = True
PAPER_RT_PRIORITY = 80
PAPER_CPU_ISOLATION_ENABLED = True
PAPER_HOUSEKEEPING_CPUS = "0"
PAPER_BENCHMARK_CPUS = "1-3"

# A camera acquisition benchmark is not allowed to carry a failed pipeline
# transition into the next logical run.  Every benchmark entry point uses these
# defaults: preserve the failed attempt, reset each selected composite RealSense
# USB device, and retry only the same logical run.
DEFAULT_RECOVER_ON_FAILURE = "full-reset"
DEFAULT_MAX_ATTEMPTS_PER_RUN = 3
DEFAULT_RECOVERY_RESET_TIMEOUT_MS = 5000
DEFAULT_RECOVERY_WAIT_SECONDS = 1.2
DEFAULT_RECOVERY_SETTLE_SECONDS = 0.0
DEFAULT_RESET_BEFORE_RUN = True

POLICY_NAMES = {
    "other": "SCHED_OTHER",
    "rr": "SCHED_RR",
    "fifo": "SCHED_FIFO",
}
