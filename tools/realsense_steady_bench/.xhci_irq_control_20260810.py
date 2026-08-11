#!/usr/bin/env python3
"""Temporary, fail-closed xHCI IRQ-thread control for the H2 campaign."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from realsense_bench_common.cpu_isolation import (  # noqa: E402
    CpuIsolation,
    CpuIsolationConfig,
    format_cpu_list,
    parse_cpu_list,
)


def controller(repo_root: Path) -> CpuIsolation:
    return CpuIsolation(
        CpuIsolationConfig(
            enabled=True,
            housekeeping_cpus="0",
            benchmark_cpus="1-3",
            use_sudo=False,
            repo_root=repo_root,
        )
    )


def irq_records(control: CpuIsolation) -> list[dict[str, Any]]:
    records = control.discover_camera_xhci_irqs()
    if len(records) != 2:
        raise RuntimeError(
            f"H2 requires exactly two camera xHCI IRQs; found {len(records)}"
        )
    for record in records:
        irq = int(record["irq"])
        affinity_path = Path(f"/proc/irq/{irq}/smp_affinity_list")
        record["affinity"] = affinity_path.read_text(encoding="utf-8").strip()
        record["thread"] = control._irq_thread_state(irq)  # noqa: SLF001
        if not record["thread"]:
            raise RuntimeError(
                f"IRQ {irq} has no schedulable IRQ thread; boot with threadirqs"
            )
    return records


def snapshot(control: CpuIsolation) -> dict[str, Any]:
    return {
        "kernel": platform.release(),
        "kernel_version": platform.version(),
        "cmdline": Path("/proc/cmdline").read_text(encoding="utf-8").strip(),
        "xhci_irqs": irq_records(control),
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify(
    state: dict[str, Any],
    cpus: str | None,
    policy: str,
    priority: int | None,
) -> None:
    realtime_path = Path("/sys/kernel/realtime")
    sysfs_preempt_rt = (
        realtime_path.is_file()
        and realtime_path.read_text(encoding="utf-8").strip() == "1"
    )
    # Some custom PREEMPT_RT Raspberry Pi kernels do not expose
    # /sys/kernel/realtime.  Retain the sysfs check, but also use the kernel
    # release and build version.  The IRQ-thread existence check in
    # irq_records() remains the fail-closed functional test.
    kernel_release = str(state["kernel"]).lower()
    kernel_version = str(state.get("kernel_version", platform.version())).lower()
    preempt_rt = (
        sysfs_preempt_rt
        or "-rt-" in f"-{kernel_release}-"
        or "preempt_rt" in kernel_version
    )
    if "threadirqs" not in state["cmdline"].split() and not preempt_rt:
        raise RuntimeError(
            "the running non-PREEMPT_RT kernel command line lacks threadirqs"
        )
    expected_cpus = parse_cpu_list(cpus) if cpus is not None else None
    errors: list[str] = []
    for record in state["xhci_irqs"]:
        thread = record["thread"]
        if (
            expected_cpus is not None
            and parse_cpu_list(record["affinity"]) != expected_cpus
        ):
            errors.append(
                f"IRQ {record['irq']} affinity={record['affinity']}, expected {cpus}"
            )
        if (
            expected_cpus is not None
            and parse_cpu_list(thread["affinity"]) != expected_cpus
        ):
            errors.append(
                f"IRQ thread {thread['pid']} affinity={thread['affinity']}, "
                f"expected {cpus}"
            )
        if policy != "keep" and thread["policy"] != policy:
            errors.append(
                f"IRQ thread {thread['pid']} policy={thread['policy']}, "
                f"expected {policy}"
            )
        if priority is not None and int(thread["priority"]) != priority:
            errors.append(
                f"IRQ thread {thread['pid']} priority={thread['priority']}, "
                f"expected {priority}"
            )
    if errors:
        raise RuntimeError(" | ".join(errors))


def configure(
    control: CpuIsolation,
    cpus: str | None,
    policy: str,
    priority: int | None,
    original_output: Path,
    effective_output: Path,
) -> None:
    original = snapshot(control)
    write_json(original_output, original)
    cpu_set = parse_cpu_list(cpus) if cpus is not None else None
    canonical_cpus = format_cpu_list(cpu_set) if cpu_set is not None else None
    for record in original["xhci_irqs"]:
        irq = int(record["irq"])
        thread = record["thread"]
        if cpu_set is not None:
            assert canonical_cpus is not None
            Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(
                canonical_cpus + "\n", encoding="utf-8"
            )
            os.sched_setaffinity(int(thread["pid"]), cpu_set)
        if policy == "SCHED_FIFO":
            assert priority is not None
            os.sched_setscheduler(
                int(thread["pid"]), os.SCHED_FIFO, os.sched_param(priority)
            )
        elif policy == "SCHED_RR":
            assert priority is not None
            os.sched_setscheduler(
                int(thread["pid"]), os.SCHED_RR, os.sched_param(priority)
            )
        elif policy == "SCHED_OTHER":
            os.sched_setscheduler(
                int(thread["pid"]), os.SCHED_OTHER, os.sched_param(0)
            )
    effective = snapshot(control)
    verify(effective, canonical_cpus, policy, priority)
    write_json(effective_output, effective)


def restore(control: CpuIsolation, original_input: Path, output: Path) -> None:
    original = json.loads(original_input.read_text(encoding="utf-8"))
    for record in original["xhci_irqs"]:
        irq = int(record["irq"])
        current = control._irq_thread_state(irq)  # noqa: SLF001
        if not current:
            raise RuntimeError(f"IRQ {irq} thread disappeared before restore")
        old_thread = record["thread"]
        Path(f"/proc/irq/{irq}/smp_affinity_list").write_text(
            str(record["affinity"]) + "\n", encoding="utf-8"
        )
        os.sched_setaffinity(
            int(current["pid"]), parse_cpu_list(str(old_thread["affinity"]))
        )
        policy_ids = {
            "SCHED_OTHER": os.SCHED_OTHER,
            "SCHED_FIFO": os.SCHED_FIFO,
            "SCHED_RR": os.SCHED_RR,
        }
        old_policy = str(old_thread["policy"])
        if old_policy not in policy_ids:
            raise RuntimeError(f"cannot restore unsupported policy {old_policy}")
        os.sched_setscheduler(
            int(current["pid"]),
            policy_ids[old_policy],
            os.sched_param(int(old_thread["priority"])),
        )
    write_json(output, snapshot(control))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "configure", "verify", "restore"))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--cpus",
        default="keep",
        help="CPU list to apply, or 'keep' to leave IRQ affinity unchanged",
    )
    parser.add_argument(
        "--policy",
        choices=("keep", "SCHED_OTHER", "SCHED_RR", "SCHED_FIFO"),
        default="keep",
    )
    parser.add_argument("--priority", type=int)
    parser.add_argument("--original-output", type=Path)
    parser.add_argument("--effective-output", type=Path)
    parser.add_argument("--original-input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    cpus = None if args.cpus == "keep" else args.cpus

    if args.policy in {"SCHED_FIFO", "SCHED_RR"} and args.priority is None:
        parser.error(f"{args.policy} requires --priority")
    if args.policy in {"keep", "SCHED_OTHER"} and args.priority is not None:
        parser.error(f"{args.policy} does not accept --priority")

    control = controller(args.repo_root)
    if args.action == "snapshot":
        if args.output is None:
            parser.error("snapshot requires --output")
        write_json(args.output, snapshot(control))
    elif args.action == "configure":
        if args.original_output is None or args.effective_output is None:
            parser.error("configure requires --original-output and --effective-output")
        configure(
            control,
            cpus,
            args.policy,
            args.priority,
            args.original_output,
            args.effective_output,
        )
    elif args.action == "verify":
        if args.output is None:
            parser.error("verify requires --output")
        state = snapshot(control)
        verify(state, cpus, args.policy, args.priority)
        write_json(args.output, state)
    else:
        if args.original_input is None or args.output is None:
            parser.error("restore requires --original-input and --output")
        restore(control, args.original_input, args.output)


if __name__ == "__main__":
    main()
