#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time


def run_command(cmd):
    try:
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        return {
            "cmd": cmd,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except FileNotFoundError as exc:
        return {"cmd": cmd, "returncode": 127, "stdout": "", "stderr": str(exc)}


def read_text(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError as exc:
        return f"ERROR: {exc}"


def parse_interrupts(raw):
    lines = []
    cpus = []
    for line in raw.splitlines():
        if line.lstrip().startswith("CPU"):
            cpus = line.split()
            continue
        if "xhci" not in line.lower() and "uvc" not in line.lower():
            continue
        m = re.match(r"\s*([^:]+):\s+(.*)", line)
        if not m:
            continue
        irq = m.group(1).strip()
        rest = m.group(2).split()
        counts = []
        idx = 0
        while idx < len(rest) and rest[idx].isdigit():
            counts.append(int(rest[idx]))
            idx += 1
        lines.append({
            "irq": irq,
            "counts": counts,
            "description": " ".join(rest[idx:]),
        })
    return {"cpus": cpus, "lines": lines}


def main():
    parser = argparse.ArgumentParser(description="Capture USB/xHCI topology and interrupt state.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--include-dmesg", action="store_true")
    args = parser.parse_args()

    interrupts_raw = read_text("/proc/interrupts")
    snapshot = {
        "timestamp_unix_ns": time.time_ns(),
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_online": read_text("/sys/devices/system/cpu/online").strip(),
        "cmd_lspci_xhci": run_command(["lspci"]),
        "cmd_lspci_xhci_filtered": run_command(["bash", "-lc", "lspci | grep -i xhci || true"]),
        "cmd_lsusb_tree": run_command(["lsusb", "-t"]),
        "cmd_lsusb": run_command(["lsusb"]),
        "proc_interrupts": interrupts_raw,
        "parsed_interrupts": parse_interrupts(interrupts_raw),
    }
    if args.include_dmesg:
        snapshot["cmd_dmesg_usb_xhci"] = run_command(["bash", "-lc", "dmesg | grep -Ei 'usb|xhci' | tail -n 200 || true"])

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    print(output)


if __name__ == "__main__":
    main()
