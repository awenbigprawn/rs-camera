#!/usr/bin/env python3
"""Plan and run topology-aware heterogeneous RealSense steady campaigns."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Mapping, Sequence


TOOL_DIR = Path(__file__).resolve().parent
TOOLS_DIR = TOOL_DIR.parent
REPO_ROOT = TOOLS_DIR.parent
STEADY_RUNNER = TOOLS_DIR / "realsense_steady_bench" / "run_steady_campaign.py"
DEFAULT_BUILD_DIR = REPO_ROOT / "build-realsense-steady"
DEFAULT_RESULTS_DIR = TOOL_DIR / "results"
DEFAULT_CAMERA_REGISTRY = TOOL_DIR / "known_cameras.json"
USB_SYSFS_BASE = Path("/sys/bus/usb/devices")
MODELED_POLICIES = {"deadline", "rr-rm", "fifo-rm"}
STEREO_STRESS_PROFILES = {
    "D435": {
        "depth_width": 848,
        "depth_height": 480,
        "color_width": 960,
        "color_height": 540,
        "fps": 60,
    },
    "D455": {
        "depth_width": 848,
        "depth_height": 480,
        "color_width": 848,
        "color_height": 480,
        "fps": 60,
    },
}
STEREO_MODELS = set(STEREO_STRESS_PROFILES)

sys.path.insert(0, str(TOOLS_DIR))

from realsense_bench_common.realsense_devices import (  # noqa: E402
    devices_by_serial,
    discover_realsense_devices,
)
from realsense_bench_common.settings import (  # noqa: E402
    DEFAULT_MAX_ATTEMPTS_PER_RUN,
    DEFAULT_RECOVER_ON_FAILURE,
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_camera_registry(path: Path) -> List[Dict[str, str]]:
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry.get("schema_version") != 1:
        raise ValueError(f"unsupported camera registry schema in {path}")
    cameras = registry.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError(f"camera registry has no cameras: {path}")
    required = (
        "label",
        "model",
        "product_id",
        "librealsense_serial",
        "usb_descriptor_serial",
    )
    normalized: List[Dict[str, str]] = []
    for camera in cameras:
        record = {key: str(value) for key, value in camera.items()}
        missing = [key for key in required if not record.get(key)]
        if missing:
            raise ValueError(
                f"camera registry entry is missing {', '.join(missing)}: {camera}"
            )
        record["model"] = record["model"].upper()
        record["product_id"] = record["product_id"].lower()
        normalized.append(record)
    for field in ("label", "librealsense_serial", "usb_descriptor_serial"):
        values = [camera[field] for camera in normalized]
        if len(values) != len(set(values)):
            raise ValueError(f"camera registry has duplicate {field} values")
    return normalized


def _camera_labels(
    devices: Sequence[Mapping[str, Any]],
    known_cameras: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    known_by_usb_serial = {
        str(camera["usb_descriptor_serial"]): camera for camera in known_cameras
    }
    cameras: List[Dict[str, Any]] = []
    for device in sorted(
        devices,
        key=lambda item: (
            str(item.get("model", "")),
            str(item.get("usb_descriptor_serial", item.get("serial", ""))),
        ),
    ):
        usb_serial = str(
            device.get("usb_descriptor_serial", device.get("serial", ""))
        )
        known = known_by_usb_serial.get(usb_serial)
        if known is None:
            raise ValueError(
                "connected RealSense camera is absent from the camera registry: "
                f"model={device.get('model', 'unknown')} "
                f"product_id={device.get('product_id', '')} "
                f"usb_descriptor_serial={usb_serial}; query its librealsense "
                "serial and add both serial namespaces to the registry"
            )
        model = str(device.get("model", "unknown")).upper()
        known_model = str(known["model"]).upper()
        known_product_id = str(known["product_id"]).lower()
        if model != known_model or str(device.get("product_id", "")).lower() != known_product_id:
            raise ValueError(
                f"{usb_serial}: registry identity is {known_model}/{known_product_id}, "
                f"but sysfs reports {model}/{device.get('product_id', '')}"
            )
        librealsense_serial = str(known["librealsense_serial"])
        cameras.append(
            {
                "label": str(known["label"]),
                "model": model,
                # Keep `serial` as the probe-facing serial for compatibility
                # with the steady benchmark case schema.
                "serial": librealsense_serial,
                "librealsense_serial": librealsense_serial,
                "usb_descriptor_serial": usb_serial,
                "product_id": str(device.get("product_id", "")),
                "product": str(device.get("product", "")),
                "usb_device": str(device.get("usb_device", "")),
                "upstream_hub": str(device.get("upstream_hub", "")),
                "xhci_controller": str(device.get("xhci_controller", "")),
                "speed_mbps": str(device.get("speed_mbps", "")),
                "power_control": str(device.get("power_control", "")),
            }
        )
    return cameras


def inventory_layout(
    usb_sysfs_base: Path,
    max_per_hub: int,
    known_cameras: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    devices = discover_realsense_devices(usb_sysfs_base)
    cameras = _camera_labels(devices, known_cameras)
    model_priority = {"D455": 0, "D435": 1, "D415": 2}
    model_counts = Counter(str(camera["model"]) for camera in cameras)
    model_order = sorted(
        model_counts,
        key=lambda model: (
            -model_counts[model],
            model_priority.get(model, 99),
            model,
        ),
    )
    scaling_order: List[str] = []
    for model in model_order:
        scaling_order.extend(
            str(camera["label"])
            for camera in _spread(
                [camera for camera in cameras if camera["model"] == model]
            )
        )
    return {
        "schema_version": 2,
        "generated_at_utc": utc_timestamp(),
        "max_cameras_per_powered_hub": max_per_hub,
        "cameras": cameras,
        "scaling_order": scaling_order,
        "notes": [
            "Keep this file with the result set: it freezes serial-to-port placement.",
            "Use no more than two cameras per 5 V / 3 A powered hub.",
            "Only cameras listed here may remain connected during this batch.",
            "Re-run inventory after physically changing any camera or hub port.",
        ],
    }


def load_layout(path: Path) -> Dict[str, Any]:
    layout = json.loads(path.read_text(encoding="utf-8"))
    if layout.get("schema_version") != 2:
        raise ValueError(f"unsupported layout schema in {path}")
    cameras = layout.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError(f"layout has no cameras: {path}")
    labels = [str(camera.get("label", "")) for camera in cameras]
    serials = [str(camera.get("librealsense_serial", "")) for camera in cameras]
    usb_serials = [str(camera.get("usb_descriptor_serial", "")) for camera in cameras]
    if any(not value for value in labels + serials + usb_serials):
        raise ValueError(
            "every layout camera needs a non-empty label, librealsense_serial, "
            "and usb_descriptor_serial"
        )
    for field, values in (
        ("labels", labels),
        ("librealsense serials", serials),
        ("USB descriptor serials", usb_serials),
    ):
        if len(set(values)) != len(values):
            raise ValueError(f"layout {field} must be unique")
    for camera in cameras:
        if str(camera.get("serial", "")) != str(camera["librealsense_serial"]):
            raise ValueError("layout serial must equal librealsense_serial")
    return layout


def validate_layout(
    layout: Mapping[str, Any],
    *,
    usb_sysfs_base: Path,
    allow_extra_cameras: bool,
    allow_topology_change: bool,
) -> Dict[str, Any]:
    connected = discover_realsense_devices(usb_sysfs_base)
    connected_by_usb_serial = devices_by_serial(connected)
    layout_cameras = list(layout["cameras"])
    layout_usb_serials = {
        str(camera["usb_descriptor_serial"]) for camera in layout_cameras
    }
    connected_usb_serials = set(connected_by_usb_serial)
    errors: List[str] = []
    warnings: List[str] = []

    missing = sorted(layout_usb_serials - connected_usb_serials)
    extra = sorted(connected_usb_serials - layout_usb_serials)
    if missing:
        errors.append(
            "layout cameras are missing by USB descriptor serial: "
            + ", ".join(missing)
        )
    if extra and not allow_extra_cameras:
        errors.append(
            "unlisted RealSense cameras are connected by USB descriptor serial: "
            + ", ".join(extra)
        )
    elif extra:
        warnings.append("extra connected cameras: " + ", ".join(extra))

    verified: List[Dict[str, Any]] = []
    for expected in layout_cameras:
        serial = str(expected["librealsense_serial"])
        usb_serial = str(expected["usb_descriptor_serial"])
        actual = connected_by_usb_serial.get(usb_serial)
        if actual is None:
            continue
        expected_model = str(expected.get("model", "")).upper()
        actual_model = str(actual.get("model", "")).upper()
        if expected_model and actual_model != expected_model:
            errors.append(
                f"{serial}: model changed from {expected_model} to {actual_model}"
            )
        for field in ("usb_device", "upstream_hub", "xhci_controller"):
            old = str(expected.get(field, ""))
            new = str(actual.get(field, ""))
            if old and new != old and not allow_topology_change:
                errors.append(f"{serial}: {field} changed from {old} to {new}")
            elif old and new != old:
                warnings.append(f"{serial}: {field} changed from {old} to {new}")
        try:
            speed_mbps = float(str(actual.get("speed_mbps", "0")))
        except ValueError:
            speed_mbps = 0.0
        if speed_mbps < 5000.0:
            errors.append(
                f"{serial}: USB speed is {actual.get('speed_mbps')!r} Mbps, "
                "expected SuperSpeed (at least 5000 Mbps)"
            )
        if actual.get("power_control") != "on":
            warnings.append(
                f"{serial}: power/control={actual.get('power_control')!r}; "
                "the steady campaign will force it to 'on' before measurement"
            )
        verified.append(
            {
                **dict(actual),
                "label": str(expected["label"]),
                "serial": serial,
                "librealsense_serial": serial,
                "usb_descriptor_serial": usb_serial,
            }
        )

    max_per_hub = int(layout.get("max_cameras_per_powered_hub", 2))
    hub_counts = Counter(
        str(device.get("upstream_hub", "unknown")) for device in verified
    )
    overloaded = {
        hub: count for hub, count in hub_counts.items() if count > max_per_hub
    }
    if overloaded:
        errors.append(
            "powered-hub camera limit exceeded: "
            + ", ".join(f"{hub}={count}" for hub, count in sorted(overloaded.items()))
        )
    controllers = sorted(
        {str(device.get("xhci_controller", "")) for device in verified}
    )
    if len(verified) > 1 and len([item for item in controllers if item]) < 2:
        warnings.append(
            "all cameras appear below one xHCI controller; record this as a "
            "shared-controller topology rather than assuming independent links"
        )

    report = {
        "success": not errors,
        "errors": errors,
        "warnings": warnings,
        "layout_camera_count": len(layout_cameras),
        "connected_camera_count": len(connected),
        "verified_cameras": verified,
        "hub_counts": dict(sorted(hub_counts.items())),
        "xhci_controllers": controllers,
    }
    if errors:
        raise RuntimeError("preflight failed: " + " | ".join(errors))
    return report


def _spread(cameras: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Order cameras so prefixes use distinct hubs before sharing a hub."""
    by_hub: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for camera in cameras:
        by_hub[str(camera.get("upstream_hub", "unknown"))].append(camera)
    for group in by_hub.values():
        group.sort(key=lambda item: (str(item["model"]), str(item["serial"])))
    result: List[Mapping[str, Any]] = []
    while any(by_hub.values()):
        for hub in sorted(by_hub):
            if by_hub[hub]:
                result.append(by_hub[hub].pop(0))
    return result


def _choose_models(
    cameras: Sequence[Mapping[str, Any]], models: Sequence[str]
) -> List[Mapping[str, Any]] | None:
    selected: List[Mapping[str, Any]] = []
    used_hubs: Counter[str] = Counter()
    for model in models:
        candidates = [
            camera
            for camera in cameras
            if str(camera["model"]).upper() == model.upper()
            and camera not in selected
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda camera: (
                used_hubs[str(camera.get("upstream_hub", ""))],
                str(camera["serial"]),
            )
        )
        choice = candidates[0]
        selected.append(choice)
        used_hubs[str(choice.get("upstream_hub", ""))] += 1
    return selected


def _physical(cameras: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    hub_counts = Counter(str(camera["upstream_hub"]) for camera in cameras)
    return {
        "camera_count": len(cameras),
        "camera_models": "+".join(str(camera["model"]) for camera in cameras),
        "camera_labels": "+".join(str(camera["label"]) for camera in cameras),
        "camera_serials": "+".join(str(camera["serial"]) for camera in cameras),
        "hub_distribution": "+".join(
            f"{hub}:{count}" for hub, count in sorted(hub_counts.items())
        ),
        "hub_label": "powered_usb3",
        "root_hub_label": "+".join(
            sorted({str(camera["xhci_controller"]) for camera in cameras})
        ),
        "usb_speed_label": "usb3_superspeed_preflight_verified",
    }


def _common_case(
    case_id: str,
    cameras: Sequence[Mapping[str, Any]],
    duration_seconds: int,
) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "workload": {
            "class": "heterogeneous_common",
            "measurement_seconds": duration_seconds,
            "depth": "848x480_Z16_30fps",
            "color": "640x480_RGB8_30fps",
            "infrared": "disabled",
        },
        "physical": _physical(cameras),
        "probe": {
            "camera_count": len(cameras),
            "serials": [str(camera["serial"]) for camera in cameras],
            "stream_mode": "depth_color",
            "delivery": "wait",
            "frames": duration_seconds * 30,
            "measurement_duration_ms": duration_seconds * 1000,
            "warmup_frames": 300,
            "frame_timeout_ms": 1500,
            "startup_timeout_ms": 15000,
            "fps": 30,
            "depth_width": 848,
            "depth_height": 480,
            "color_width": 640,
            "color_height": 480,
        },
    }


def _stereo_case(
    case_id: str,
    cameras: Sequence[Mapping[str, Any]],
    duration_seconds: int,
) -> Dict[str, Any]:
    models = {str(camera["model"]).upper() for camera in cameras}
    invalid = sorted(models - STEREO_MODELS)
    if invalid:
        raise ValueError(
            "stereo_all is reserved for D435/D455; invalid: " + ", ".join(invalid)
        )
    if len(models) != 1:
        raise ValueError(
            "stereo stress profiles are model-specific; split mixed models "
            "into homogeneous cases"
        )
    model = next(iter(models))
    profile = STEREO_STRESS_PROFILES[model]
    depth_width = int(profile["depth_width"])
    depth_height = int(profile["depth_height"])
    color_width = int(profile["color_width"])
    color_height = int(profile["color_height"])
    fps = int(profile["fps"])
    return {
        "case_id": case_id,
        "workload": {
            "class": "stereo_stress",
            "measurement_seconds": duration_seconds,
            "depth": f"{depth_width}x{depth_height}_Z16_{fps}fps",
            "color": f"{color_width}x{color_height}_RGB8_{fps}fps",
            "infrared": (
                f"IR1+IR2_{depth_width}x{depth_height}_Y8_{fps}fps"
            ),
        },
        "physical": _physical(cameras),
        "probe": {
            "camera_count": len(cameras),
            "serials": [str(camera["serial"]) for camera in cameras],
            # Retain the compatibility name for older probe builds.
            "stream_mode": "d435_all",
            "delivery": "wait",
            "frames": duration_seconds * fps,
            "measurement_duration_ms": duration_seconds * 1000,
            "warmup_frames": fps * 10,
            "frame_timeout_ms": 1500,
            "startup_timeout_ms": 15000,
            "fps": fps,
            "depth_width": depth_width,
            "depth_height": depth_height,
            "color_width": color_width,
            "color_height": color_height,
        },
    }


def _case_suffix(cameras: Sequence[Mapping[str, Any]]) -> str:
    return "-".join(str(camera["label"]) for camera in cameras)


def plan_cases(
    layout: Mapping[str, Any], suite: str, duration_seconds: int
) -> List[Dict[str, Any]]:
    cameras = list(layout["cameras"])
    cases: List[Dict[str, Any]] = []

    if suite in {"smoke", "all"}:
        for camera in cameras:
            cases.append(
                _common_case(
                    f"smoke_common30_{camera['label']}",
                    [camera],
                    duration_seconds,
                )
            )

    if suite in {"scaling", "all"}:
        by_label = {str(camera["label"]): camera for camera in cameras}
        configured_order = [
            str(label) for label in layout.get("scaling_order", [])
        ]
        if len(configured_order) != len(set(configured_order)):
            raise ValueError("layout scaling_order contains duplicate labels")
        unknown = sorted(set(configured_order) - set(by_label))
        if unknown:
            raise ValueError(
                "layout scaling_order contains unknown labels: " + ", ".join(unknown)
            )
        ordered = [by_label[label] for label in configured_order]
        ordered.extend(
            camera for camera in _spread(cameras) if camera not in ordered
        )
        for count in range(1, min(4, len(ordered)) + 1):
            selected = ordered[:count]
            cases.append(
                _common_case(
                    f"scaling_common30_n{count}_{_case_suffix(selected)}",
                    selected,
                    duration_seconds,
                )
            )

    if suite in {"heterogeneous", "all"}:
        models = sorted({str(camera["model"]).upper() for camera in cameras})
        selected_sets: List[List[Mapping[str, Any]]] = []
        for left, right in itertools.combinations(models, 2):
            selected = _choose_models(cameras, [left, right])
            if selected:
                selected_sets.append(selected)
        if {"D455", "D435", "D415"}.issubset(models):
            selected = _choose_models(cameras, ["D455", "D435", "D415"])
            if selected:
                selected_sets.append(selected)
        if len(cameras) >= 4 and len(models) >= 2:
            selected_sets.append(_spread(cameras)[:4])
        seen = set()
        for selected in selected_sets:
            serial_key = tuple(sorted(str(camera["serial"]) for camera in selected))
            if serial_key in seen:
                continue
            seen.add(serial_key)
            composition = "-".join(str(camera["model"]).lower() for camera in selected)
            cases.append(
                _common_case(
                    f"heterogeneous_common30_{composition}_{_case_suffix(selected)}",
                    selected,
                    duration_seconds,
                )
            )

    if suite in {"stereo-smoke", "stereo-stress", "all"}:
        for model in ("D455", "D435"):
            matching = [
                camera for camera in cameras if str(camera["model"]).upper() == model
            ]
            ordered = _spread(matching)
            # Qualify every physical camera independently.  Prefix-only
            # scaling would otherwise test just one singleton and could hide
            # a device-specific startup or thermal fault.
            for camera in ordered:
                cases.append(
                    _stereo_case(
                        f"{model.lower()}_stress60_n1_{camera['label']}",
                        [camera],
                        duration_seconds,
                    )
                )
            for count in range(2, min(4, len(ordered)) + 1):
                selected = ordered[:count]
                cases.append(
                    _stereo_case(
                        f"{model.lower()}_stress60_n{count}_{_case_suffix(selected)}",
                        selected,
                        duration_seconds,
                    )
                )

    deduplicated: Dict[tuple[Any, ...], Dict[str, Any]] = {}
    for case in cases:
        probe = case["probe"]
        signature = (
            tuple(sorted(str(serial) for serial in probe["serials"])),
            str(probe["stream_mode"]),
            int(probe["fps"]),
            int(probe["depth_width"]),
            int(probe["depth_height"]),
            int(probe["color_width"]),
            int(probe["color_height"]),
        )
        deduplicated.setdefault(signature, case)
    return list(deduplicated.values())


def _profile_cases(
    cases: Iterable[Dict[str, Any]], profiles_dir: Path
) -> None:
    missing = []
    for case in cases:
        profile = (profiles_dir / f"{case['case_id']}.csv").resolve()
        if not profile.is_file():
            missing.append(str(profile))
        else:
            case["probe"]["deadline_profile"] = str(profile)
    if missing:
        raise ValueError(
            "one scheduler profile per exact camera case is required: "
            + ", ".join(missing)
        )


def _print_inventory(layout: Mapping[str, Any]) -> None:
    print(
        "label\tmodel\tlibrealsense_serial\tusb_descriptor_serial\t"
        "usb\thub\txhci\tspeed\tpower"
    )
    for camera in layout["cameras"]:
        print(
            "\t".join(
                str(camera.get(key, ""))
                for key in (
                    "label",
                    "model",
                    "librealsense_serial",
                    "usb_descriptor_serial",
                    "usb_device",
                    "upstream_hub",
                    "xhci_controller",
                    "speed_mbps",
                    "power_control",
                )
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory", help="capture connected cameras and their physical placement"
    )
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--max-cameras-per-hub", type=int, default=2)
    inventory.add_argument("--usb-sysfs-base", type=Path, default=USB_SYSFS_BASE)
    inventory.add_argument(
        "--camera-registry",
        type=Path,
        default=DEFAULT_CAMERA_REGISTRY,
        help="known optical/ASIC serial mapping",
    )

    run = subparsers.add_parser(
        "run", help="validate a frozen layout and invoke the steady benchmark"
    )
    run.add_argument("--layout", type=Path, required=True)
    run.add_argument(
        "--suite",
        choices=(
            "smoke",
            "scaling",
            "heterogeneous",
            "stereo-smoke",
            "stereo-stress",
            "all",
        ),
        required=True,
    )
    run.add_argument("--duration-seconds", type=int)
    run.add_argument("--nb-runs", type=int)
    run.add_argument(
        "--policies",
        nargs="+",
        choices=("other", "rr", "fifo", "deadline", "rr-rm", "fifo-rm"),
        default=["other"],
    )
    run.add_argument("--profiles-dir", type=Path)
    run.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    run.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    run.add_argument("--build-jobs", type=int, default=3)
    run.add_argument("--no-lime", action="store_true")
    run.add_argument("--no-sudo", action="store_true")
    run.add_argument("--no-cpu-isolation", action="store_true")
    run.add_argument("--allow-extra-cameras", action="store_true")
    run.add_argument("--allow-topology-change", action="store_true")
    run.add_argument("--usb-sysfs-base", type=Path, default=USB_SYSFS_BASE)
    run.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if args.command == "inventory":
        if args.max_cameras_per_hub < 1:
            raise SystemExit("--max-cameras-per-hub must be positive")
        known_cameras = load_camera_registry(args.camera_registry.resolve())
        layout = inventory_layout(
            args.usb_sysfs_base,
            args.max_cameras_per_hub,
            known_cameras,
        )
        if not layout["cameras"]:
            raise SystemExit("no connected Intel RealSense camera was found")
        _json_write(args.output, layout)
        _print_inventory(layout)
        print(f"layout={args.output.resolve()}")
        return

    if args.build_jobs < 1:
        raise SystemExit("--build-jobs must be positive")
    duration_seconds = args.duration_seconds
    if duration_seconds is None:
        duration_seconds = (
            60 if args.suite in {"smoke", "stereo-smoke"} else 600
        )
    nb_runs = args.nb_runs
    if nb_runs is None:
        nb_runs = 1 if args.suite in {"smoke", "stereo-smoke"} else 3
    if duration_seconds < 1 or nb_runs < 1:
        raise SystemExit("duration and repetitions must be positive")

    layout = load_layout(args.layout.resolve())
    try:
        preflight = validate_layout(
            layout,
            usb_sysfs_base=args.usb_sysfs_base,
            allow_extra_cameras=args.allow_extra_cameras,
            allow_topology_change=args.allow_topology_change,
        )
        cases = plan_cases(layout, args.suite, duration_seconds)
        if not cases:
            raise ValueError(f"suite {args.suite!r} produced no cases")
        modeled = MODELED_POLICIES.intersection(args.policies)
        if modeled:
            if args.profiles_dir is None:
                raise ValueError(
                    "modeled policies require --profiles-dir; profiles are "
                    "specific to the exact model/count/topology case"
                )
            _profile_cases(cases, args.profiles_dir.resolve())
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error

    run_root = args.results_dir.resolve() / (
        f"multicamera_{args.suite}_{utc_timestamp()}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    config_path = run_root / "generated_cases.json"
    _json_write(
        config_path,
        {
            "description": "Generated topology-aware multi-camera steady suite",
            "source_layout": str(args.layout.resolve()),
            "suite": args.suite,
            "cases": cases,
        },
    )
    _json_write(run_root / "layout.json", layout)
    _json_write(run_root / "preflight.json", preflight)

    for warning in preflight["warnings"]:
        print(f"[PREFLIGHT-WARNING] {warning}")
    print(
        f"[PLAN] suite={args.suite} cases={len(cases)} policies={args.policies} "
        f"runs={nb_runs} duration={duration_seconds}s"
    )
    command = [
        sys.executable,
        str(STEADY_RUNNER),
        "--config",
        str(config_path),
        "--policies",
        *args.policies,
        "--nb-runs",
        str(nb_runs),
        "--build-dir",
        str(args.build_dir.resolve()),
        "--results-dir",
        str(run_root / "benchkit"),
        "--recover-on-failure",
        DEFAULT_RECOVER_ON_FAILURE,
        "--reset-before-run",
        "--max-attempts-per-run",
        str(DEFAULT_MAX_ATTEMPTS_PER_RUN),
        "--recovery-reset-timeout-ms",
        "10000",
        "--recovery-wait-seconds",
        "5",
        "--recovery-settle-seconds",
        "0",
        "--build-jobs",
        str(args.build_jobs),
    ]
    if args.no_lime:
        command.append("--no-lime")
    if args.no_sudo:
        command.append("--no-sudo")
    if args.no_cpu_isolation:
        command.append("--no-cpu-isolation")
    _json_write(
        run_root / "invocation.json",
        {
            "command": command,
            "dry_run": args.dry_run,
            "case_ids": [case["case_id"] for case in cases],
        },
    )
    print(shlex.join(command))
    print(f"artifacts={run_root}")
    if not args.dry_run:
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
