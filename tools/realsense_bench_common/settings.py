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

POLICY_NAMES = {
    "other": "SCHED_OTHER",
    "rr": "SCHED_RR",
    "fifo": "SCHED_FIFO",
}
