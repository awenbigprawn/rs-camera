"""RealSense USB-device discovery shared by benchmark controls and preflight."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List


INTEL_VENDOR_ID = "8086"
KNOWN_D400_PRODUCTS = {
    "0ad3": "D415",
    "0b07": "D435",
    "0b5c": "D455",
}


def read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_realsense_video_device(path: Path) -> bool:
    """Return true for the composite USB device of a RealSense camera."""
    if read_optional_text(path / "idVendor").lower() != INTEL_VENDOR_ID:
        return False
    product_id = read_optional_text(path / "idProduct").lower()
    identity = " ".join(
        (
            read_optional_text(path / "manufacturer"),
            read_optional_text(path / "product"),
        )
    ).lower()
    return product_id in KNOWN_D400_PRODUCTS or "realsense" in identity


def model_from_device(path: Path) -> str:
    product_id = read_optional_text(path / "idProduct").lower()
    known = KNOWN_D400_PRODUCTS.get(product_id)
    if known:
        return known
    product = read_optional_text(path / "product")
    for model in ("D455", "D435", "D415"):
        if model.lower() in product.lower():
            return model
    return product or "unknown"


def upstream_hub_name(usb_device: str) -> str:
    """Infer the immediate external-hub port path from a Linux USB port name."""
    if "." not in usb_device:
        return f"direct:{usb_device}"
    return usb_device.rsplit(".", 1)[0]


def device_record(path: Path) -> Dict[str, Any]:
    resolved = path.resolve()
    usb_descriptor_serial = read_optional_text(path / "serial")
    controller = next(
        (
            candidate.name
            for candidate in (resolved, *resolved.parents)
            if candidate.name.startswith("xhci-hcd.")
        ),
        "",
    )
    return {
        "usb_device": path.name,
        "sysfs_path": str(path),
        "resolved_sysfs_path": str(resolved),
        "vendor_id": read_optional_text(path / "idVendor").lower(),
        "product_id": read_optional_text(path / "idProduct").lower(),
        "manufacturer": read_optional_text(path / "manufacturer"),
        "product": read_optional_text(path / "product"),
        "model": model_from_device(path),
        # Linux sysfs exposes the USB descriptor iSerialNumber here.  For D400
        # cameras this is the ASIC serial, not RS2_CAMERA_INFO_SERIAL_NUMBER
        # (the optical-module serial used by librealsense camera selection).
        "serial": usb_descriptor_serial,
        "usb_descriptor_serial": usb_descriptor_serial,
        "speed_mbps": read_optional_text(path / "speed"),
        "power_control": read_optional_text(path / "power" / "control"),
        "runtime_status": read_optional_text(path / "power" / "runtime_status"),
        "upstream_hub": upstream_hub_name(path.name),
        "xhci_controller": controller,
    }


def discover_realsense_devices(usb_sysfs_base: Path) -> List[Dict[str, Any]]:
    devices = []
    for path in sorted(usb_sysfs_base.glob("*")):
        if path.is_dir() and is_realsense_video_device(path):
            devices.append(device_record(path))
    return devices


def devices_by_serial(
    devices: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for device in devices:
        serial = str(device.get("serial", ""))
        if serial:
            result[serial] = device
    return result
