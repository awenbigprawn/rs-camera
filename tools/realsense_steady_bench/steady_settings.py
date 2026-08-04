"""Steady-specific paths and experimental factor names."""

from pathlib import Path

from realsense_bench_common.settings import (
    BENCHKIT_PATH,
    CPU_LOCK_SCRIPT as CPU_LOCK,
    CPU_RESTORE_SCRIPT as CPU_RESTORE,
    DEFAULT_LIME,
    PAPER_BACKEND as CAMPAIGN_BACKEND,
    PAPER_CPU_FREQUENCY_MHZ as CAMPAIGN_CPU_FREQUENCY_MHZ,
    PAPER_DROP_CACHES_BEFORE_RUN as CAMPAIGN_DROP_CACHES_BEFORE_RUN,
    PAPER_RT_PRIORITY as CAMPAIGN_RT_PRIORITY,
    PAPER_USB_KERNEL_DRIVER as CAMPAIGN_USB_KERNEL_DRIVER,
    POLICY_NAMES as BASE_POLICY_NAMES,
    REPO_ROOT,
    RSUSB_HELPER,
    TOOLS_DIR,
    TRACER_SOURCE,
)


TOOL_DIR = TOOLS_DIR / "realsense_steady_bench"
POLICY_NAMES = {
    **BASE_POLICY_NAMES,
    "deadline": "SCHED_DEADLINE",
    "rr-rm": "SCHED_RR_RM",
    "fifo-rm": "SCHED_FIFO_RM",
}
MODELED_POLICIES = frozenset({"deadline", "rr-rm", "fifo-rm"})
DEFAULT_BUILD_DIR = REPO_ROOT / "build-realsense-steady"
DEFAULT_RESULTS_DIR = TOOL_DIR / "results"
NCNN_MODEL_PARAM = (
    REPO_ROOT / "deps" / "ncnn" / "benchmark" / "models" / "mobilenet_v2.param"
)
DEFAULT_BROADCOM_VULKAN_ICD = Path(
    "/usr/share/vulkan/icd.d/broadcom_icd.json"
)


# These paper controls are recorded but are not Cartesian factors.
CAMPAIGN_RSUSB_USB_DEVICES: tuple[str, ...] = ()
FIXED_CAMPAIGN_CONSTANTS = {
    "fixed_librealsense_backend": CAMPAIGN_BACKEND,
    "fixed_usb_kernel_driver": CAMPAIGN_USB_KERNEL_DRIVER,
    "fixed_cpu_frequency_mhz": CAMPAIGN_CPU_FREQUENCY_MHZ,
    "fixed_drop_caches_before_run": CAMPAIGN_DROP_CACHES_BEFORE_RUN,
    "fixed_realsense_usb_autosuspend_disabled": True,
    "fixed_rt_priority": CAMPAIGN_RT_PRIORITY,
}


GPU_NOISE_MODES = ("none", "mobilenet_v2_vulkan")
USB_STORAGE_NOISE_MODES = ("none", "sequential_read")
CPU_NOISE_MODES = ("none", "busy_loop")
MEMORY_NOISE_MODES = ("none", "fixed_copy")
