#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(".")
BENCHKIT_PATH = Path("deps") / "benchkit"
TOOL_DIR = Path("tools") / "realsense_usb_topology_bench"
DEFAULT_BUILD_DIR = Path("build-realsense-thread-trace")
DEFAULT_RESULTS_DIR = TOOL_DIR / "results"

if not BENCHKIT_PATH.exists():
    raise SystemExit("Run this script from the repository root; deps/benchkit was not found.")
if str(BENCHKIT_PATH) not in sys.path:
    sys.path.insert(0, str(BENCHKIT_PATH))

from benchkit.benchmark import Benchmark
from benchkit.campaign import CampaignCartesianProduct


def load_cases(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        cases = data.get("cases", [])
    else:
        cases = data
    if not isinstance(cases, list):
        raise ValueError(f"{path} does not contain a case list")
    seen = set()
    for case in cases:
        case_id = case.get("case_id")
        if not case_id:
            raise ValueError(f"case without case_id: {case}")
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
    return cases


def relative_to_repo(path: Path) -> Path:
    try:
        return path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return path


def flatten(prefix: str, value: Any, out: Dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            flatten(f"{prefix}_{key}" if prefix else key, child, out)
    elif isinstance(value, list):
        out[prefix] = json.dumps(value, sort_keys=True)
    else:
        out[prefix] = value


def interrupt_totals(snapshot_path: Path) -> Dict[str, int]:
    if not snapshot_path.exists():
        return {}
    data = json.loads(snapshot_path.read_text())
    totals = {}
    for line in data.get("parsed_interrupts", {}).get("lines", []):
        desc = line.get("description", "")
        irq = line.get("irq", "")
        totals[f"{irq}:{desc}"] = sum(int(v) for v in line.get("counts", []))
    return totals


class RealSenseUsbTopologyBench(Benchmark):
    def __init__(self, cases: Iterable[Dict[str, Any]], build_dir: Path, sudo: bool = False) -> None:
        super().__init__(
            command_wrappers=(),
            command_attachments=(),
            shared_libs=(),
            pre_run_hooks=(),
            post_run_hooks=(),
        )
        self._cases = {case["case_id"]: case for case in cases}
        self._build_dir = build_dir
        self._sudo = sudo
        self._probe = self._build_dir / "realsense_usb_latency_probe"

    @property
    def bench_src_path(self) -> Path:
        return TOOL_DIR

    @staticmethod
    def get_build_var_names() -> List[str]:
        return []

    @staticmethod
    def get_run_var_names() -> List[str]:
        return ["case_id"]

    def prebuild_bench(self, **_kwargs) -> int:
        subprocess.check_call(["cmake", "-S", str(REPO_ROOT), "-B", str(self._build_dir), "-DCMAKE_BUILD_TYPE=RelWithDebInfo"])
        subprocess.check_call(["cmake", "--build", str(self._build_dir), "--target", "realsense_usb_latency_probe", "-j4"])
        return 0

    def build_bench(self, **_kwargs) -> None:
        return None

    def _case_command(self, case: Dict[str, Any], result_json: Path) -> List[str]:
        probe = case.get("probe", {})
        cmd = [str(self._probe)]
        for key, flag in [
            ("serial", "--serial"),
            ("stream_mode", "--stream-mode"),
            ("delivery", "--delivery"),
            ("width", "--width"),
            ("height", "--height"),
            ("fps", "--fps"),
            ("duration_sec", "--duration-sec"),
            ("warmup_frames", "--warmup-frames"),
            ("timeout_ms", "--timeout-ms"),
        ]:
            if key in probe and probe[key] not in (None, ""):
                cmd += [flag, str(probe[key])]
        cmd += ["--output", str(relative_to_repo(result_json))]
        if self._sudo:
            cmd = ["sudo", "--preserve-env=LD_LIBRARY_PATH"] + cmd
        return cmd

    def _snapshot(self, output_path: Path) -> None:
        subprocess.run(
            [sys.executable, str(TOOL_DIR / "snapshot_topology.py"), "--output", str(relative_to_repo(output_path))],
            check=False,
        )

    def single_run(self, case_id: str, record_data_dir: Path, **kwargs) -> str:
        case = self._cases[case_id]
        record_dir = Path(record_data_dir)
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / "case.json").write_text(json.dumps(case, indent=2, sort_keys=True))

        before = record_dir / "topology_before.json"
        after = record_dir / "topology_after.json"
        result_json = record_dir / "probe_result.json"
        self._snapshot(before)

        cmd = self._case_command(case, result_json)
        env = self._preload_env(**kwargs)
        wrapped_cmd, wrapped_env = self._wrap_command(
            run_command=cmd,
            environment=env,
            **kwargs,
        )
        timeout = int(case.get("probe", {}).get("duration_sec", 30)) + 60
        output = self.run_bench_command(
            run_command=cmd,
            wrapped_run_command=wrapped_cmd,
            current_dir=REPO_ROOT,
            environment=env,
            wrapped_environment=wrapped_env,
            print_output=False,
            timeout=timeout,
        )
        self._snapshot(after)
        (record_dir / "probe_stdout.json").write_text(output)
        return output

    def parse_output_to_results(
        self,
        command_output: str,
        build_variables: Dict[str, Any],
        run_variables: Dict[str, Any],
        benchmark_duration_seconds: int,
        record_data_dir: Path,
        **kwargs,
    ) -> Dict[str, Any]:
        record_dir = Path(record_data_dir)
        result_path = record_dir / "probe_result.json"
        data = json.loads(result_path.read_text() if result_path.exists() else command_output)
        case = self._cases[run_variables["case_id"]]

        results: Dict[str, Any] = {}
        flatten("probe", case.get("probe", {}), results)
        flatten("physical", case.get("physical", {}), results)
        flatten("device", data.get("device", {}), results)
        flatten("run", data.get("run", {}), results)
        summary = data.get("summary", {})
        for key in ["framesets", "callbacks", "frames", "drops", "timeouts"]:
            results[key] = summary.get(key, 0)
        for metric_name in ["wait_ms", "frameset_interarrival_ms", "callback_gap_ms"]:
            flatten(metric_name, summary.get(metric_name, {}), results)

        streams = summary.get("streams", {})
        for stream_name, stream in streams.items():
            prefix = "stream_" + stream_name.lower().replace(" ", "_").replace("#", "_")
            results[f"{prefix}_frames"] = stream.get("frames", 0)
            results[f"{prefix}_drops"] = stream.get("drops", 0)
            results[f"{prefix}_timestamp_domain"] = stream.get("timestamp_domain", "")
            flatten(f"{prefix}_sensor_interarrival_ms", stream.get("sensor_interarrival_ms", {}), results)
            flatten(f"{prefix}_host_interarrival_ms", stream.get("host_interarrival_ms", {}), results)

        before_totals = interrupt_totals(record_dir / "topology_before.json")
        after_totals = interrupt_totals(record_dir / "topology_after.json")
        irq_delta = {
            key: after_totals.get(key, 0) - before_totals.get(key, 0)
            for key in sorted(set(before_totals) | set(after_totals))
        }
        results["irq_delta_json"] = json.dumps(irq_delta, sort_keys=True)
        results["record_data_dir"] = str(relative_to_repo(record_dir))
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RealSense USB topology timing campaign with benchkit.")
    parser.add_argument("--config", type=Path, default=TOOL_DIR / "configs" / "smoke_matrix.json")
    parser.add_argument("--case", dest="cases", action="append", help="Run one case_id; can be repeated.")
    parser.add_argument("--nb-runs", type=int, default=1)
    parser.add_argument("--duration-sec", type=int, help="Override duration_sec for all cases.")
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--sudo", action="store_true", help="Run the probe through sudo, useful for RT scheduling later.")
    args = parser.parse_args()

    cases = load_cases(args.config)
    if args.cases:
        wanted = set(args.cases)
        cases = [case for case in cases if case["case_id"] in wanted]
        missing = wanted - {case["case_id"] for case in cases}
        if missing:
            raise SystemExit(f"Unknown case_id(s): {', '.join(sorted(missing))}")
    if args.duration_sec is not None:
        for case in cases:
            case.setdefault("probe", {})["duration_sec"] = args.duration_sec
    if not cases:
        raise SystemExit("No cases selected")

    args.results_dir.mkdir(parents=True, exist_ok=True)
    campaign = CampaignCartesianProduct(
        name="realsense_usb_topology",
        benchmark=RealSenseUsbTopologyBench(cases=cases, build_dir=args.build_dir, sudo=args.sudo),
        nb_runs=args.nb_runs,
        variables={"case_id": [case["case_id"] for case in cases]},
        constants=None,
        debug=False,
        gdb=False,
        enable_data_dir=True,
        continuing=False,
        benchmark_duration_seconds=None,
        results_dir=str(args.results_dir),
    )
    campaign.run()


if __name__ == "__main__":
    main()
