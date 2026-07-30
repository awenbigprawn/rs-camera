"""Shared paths, fixed controls, and factor names for the steady campaign."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "realsense_steady_bench"
TOOLS_DIR = REPO_ROOT / "tools"
BENCHKIT_PATH = REPO_ROOT / "deps" / "benchkit"
DEFAULT_BUILD_DIR = REPO_ROOT / "build-realsense-steady"
DEFAULT_RESULTS_DIR = TOOL_DIR / "results"
DEFAULT_LIME = REPO_ROOT / "deps" / "lime-rtw" / "target" / "release" / "lime-rtw"
TRACER_SOURCE = REPO_ROOT / "tools" / "realsense_thread_trace" / "trace_pthreads.c"
CPU_LOCK = REPO_ROOT / "scripts" / "lock_cpu_freq.sh"
CPU_RESTORE = REPO_ROOT / "scripts" / "restore_cpu_freq_default.sh"
RSUSB_HELPER = REPO_ROOT / "scripts" / "realsense_rsusb_uvc.sh"
NCNN_MODEL_PARAM = (
    REPO_ROOT / "deps" / "ncnn" / "benchmark" / "models" / "mobilenet_v2.param"
)
DEFAULT_BROADCOM_VULKAN_ICD = Path(
    "/usr/share/vulkan/icd.d/broadcom_icd.json"
)


# Fixed controls for the paper campaign. They are recorded as Benchkit
# constants and in each run manifest, but are not experimental factors.
CAMPAIGN_BACKEND = "v4l2"
CAMPAIGN_USB_KERNEL_DRIVER = "uvcvideo"
CAMPAIGN_CPU_FREQUENCY_MHZ = 1500
CAMPAIGN_DROP_CACHES_BEFORE_RUN = True
CAMPAIGN_RT_PRIORITY = 80
CAMPAIGN_RSUSB_USB_DEVICES: tuple[str, ...] = ()
FIXED_CAMPAIGN_CONSTANTS = {
    "fixed_librealsense_backend": CAMPAIGN_BACKEND,
    "fixed_usb_kernel_driver": CAMPAIGN_USB_KERNEL_DRIVER,
    "fixed_cpu_frequency_mhz": CAMPAIGN_CPU_FREQUENCY_MHZ,
    "fixed_drop_caches_before_run": CAMPAIGN_DROP_CACHES_BEFORE_RUN,
    "fixed_rt_priority": CAMPAIGN_RT_PRIORITY,
}


POLICY_NAMES = {
    "other": "SCHED_OTHER",
    "rr": "SCHED_RR",
    "fifo": "SCHED_FIFO",
}
GPU_NOISE_MODES = ("none", "mobilenet_v2_vulkan")
USB_STORAGE_NOISE_MODES = ("none", "sequential_read")
CPU_NOISE_MODES = ("none", "busy_loop")
MEMORY_NOISE_MODES = ("none", "fixed_copy")
