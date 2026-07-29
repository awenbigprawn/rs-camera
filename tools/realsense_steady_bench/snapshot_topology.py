#!/usr/bin/env python3
"""Capture the host, USB topology, RealSense power state, and USB IRQ counters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import re
import subprocess
import time
from typing import Any, Dict, List


def command(arguments: List[str]) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            arguments,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return {
            "cmd": arguments,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except FileNotFoundError as error:
        return {
            "cmd": arguments,
            "returncode": 127,
            "stdout": "",
            "stderr": str(error),
        }


def text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError as error:
        return f"ERROR: {error}"


def parse_interrupts(raw: str) -> Dict[str, Any]:
    cpus: List[str] = []
    lines: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        if line.lstrip().startswith("CPU"):
            cpus = line.split()
            continue
        if "xhci" not in line.lower() and "uvc" not in line.lower():
            continue
        match = re.match(r"\s*([^:]+):\s+(.*)", line)
        if not match:
            continue
        fields = match.group(2).split()
        counts = []
        while fields and fields[0].isdigit():
            counts.append(int(fields.pop(0)))
        lines.append(
            {
                "irq": match.group(1).strip(),
                "counts": counts,
                "description": " ".join(fields),
            }
        )
    return {"cpus": cpus, "lines": lines}


def realsense_usb_devices() -> List[Dict[str, str]]:
    devices = []
    for path in sorted(Path("/sys/bus/usb/devices").glob("*")):
        if text(path / "idVendor").strip().lower() != "8086":
            continue
        product = text(path / "product").strip()
        manufacturer = text(path / "manufacturer").strip()
        if "realsense" not in f"{manufacturer} {product}".lower():
            continue
        devices.append(
            {
                "sysfs_name": path.name,
                "vendor": text(path / "idVendor").strip(),
                "product_id": text(path / "idProduct").strip(),
                "manufacturer": manufacturer,
                "product": product,
                "serial": text(path / "serial").strip(),
                "speed_mbps": text(path / "speed").strip(),
                "power_control": text(path / "power" / "control").strip(),
                "runtime_status": text(path / "power" / "runtime_status").strip(),
            }
        )
    return devices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-dmesg", action="store_true")
    args = parser.parse_args()

    interrupts = text(Path("/proc/interrupts"))
    lspci = command(["lspci"])
    snapshot: Dict[str, Any] = {
        "timestamp_unix_ns": time.time_ns(),
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_online": text(Path("/sys/devices/system/cpu/online")).strip(),
        "cmd_lspci": lspci,
        "lspci_xhci_lines": [
            line for line in lspci["stdout"].splitlines() if "xhci" in line.lower()
        ],
        "cmd_lsusb_tree": command(["lsusb", "-t"]),
        "cmd_lsusb": command(["lsusb"]),
        "realsense_usb_devices": realsense_usb_devices(),
        "proc_interrupts": interrupts,
        "parsed_interrupts": parse_interrupts(interrupts),
    }
    if args.include_dmesg:
        dmesg = command(["dmesg"])
        snapshot["dmesg_usb_xhci_tail"] = "\n".join(
            line
            for line in dmesg["stdout"].splitlines()
            if "usb" in line.lower() or "xhci" in line.lower() or "uvc" in line.lower()
        )[-20000:]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
