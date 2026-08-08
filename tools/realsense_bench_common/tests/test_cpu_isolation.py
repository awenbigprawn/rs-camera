#!/usr/bin/env python3

from pathlib import Path
import sys
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS_DIR))

from realsense_bench_common.cpu_isolation import (  # noqa: E402
    CpuIsolation,
    CpuIsolationConfig,
    format_cpu_list,
    parse_cpu_list,
)


class CpuListTest(unittest.TestCase):
    def test_parse_and_format_linux_cpu_list(self):
        cpus = parse_cpu_list("0,2-4,7")
        self.assertEqual(cpus, {0, 2, 3, 4, 7})
        self.assertEqual(format_cpu_list(cpus), "0,2-4,7")

    def test_rejects_descending_range(self):
        with self.assertRaisesRegex(ValueError, "descending"):
            parse_cpu_list("3-1")


class CameraIrqDiscoveryTest(unittest.TestCase):
    def test_discovers_one_irq_per_camera_xhci_controller(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            usb_sysfs = root / "usb-devices"
            devices = root / "devices"
            proc_root = root / "proc"
            usb_sysfs.mkdir()
            proc_root.mkdir()

            for index, (usb_device, controller_name, roots, product_id) in enumerate(
                (
                    ("3-1", "xhci-hcd.0", ("usb2", "usb3"), "0b5c"),
                    ("5-1", "xhci-hcd.1", ("usb4", "usb5"), "0ad3"),
                )
            ):
                controller = devices / controller_name
                for usb_root in roots:
                    (controller / usb_root).mkdir(parents=True)
                target = controller / roots[-1] / usb_device
                target.mkdir()
                (target / "idVendor").write_text("8086\n", encoding="utf-8")
                (target / "idProduct").write_text(product_id + "\n", encoding="utf-8")
                (target / "serial").write_text(
                    f"usb-serial-{index}\n", encoding="utf-8"
                )
                (usb_sysfs / usb_device).symlink_to(target, target_is_directory=True)

            (proc_root / "interrupts").write_text(
                "           CPU0 CPU1 CPU2 CPU3\n"
                "132: 10 0 0 0 chip Edge xhci-hcd:usb2\n"
                "137: 11 0 0 0 chip Edge xhci-hcd:usb4\n",
                encoding="utf-8",
            )
            isolation = CpuIsolation(
                CpuIsolationConfig(
                    enabled=True,
                    housekeeping_cpus="0",
                    benchmark_cpus="1-3",
                    use_sudo=False,
                    repo_root=root,
                    proc_root=proc_root,
                    usb_sysfs_base=usb_sysfs,
                )
            )

            records = isolation.discover_camera_xhci_irqs()

            self.assertEqual([record["irq"] for record in records], [132, 137])
            self.assertEqual(records[0]["action"], "xhci-hcd:usb2")
            self.assertEqual(records[0]["usb_devices"], ["3-1"])
            self.assertEqual(records[1]["action"], "xhci-hcd:usb4")

    def test_rejects_non_xhci_camera_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            usb_sysfs = root / "usb-devices"
            proc_root = root / "proc"
            camera = usb_sysfs / "3-1"
            camera.mkdir(parents=True)
            proc_root.mkdir()
            (camera / "idVendor").write_text("8086\n", encoding="utf-8")
            (camera / "idProduct").write_text("0b07\n", encoding="utf-8")
            (proc_root / "interrupts").write_text("", encoding="utf-8")
            isolation = CpuIsolation(
                CpuIsolationConfig(
                    enabled=True,
                    housekeeping_cpus="0",
                    benchmark_cpus="1-3",
                    use_sudo=False,
                    repo_root=root,
                    proc_root=proc_root,
                    usb_sysfs_base=usb_sysfs,
                )
            )
            with self.assertRaisesRegex(RuntimeError, "cannot find the xHCI"):
                isolation.discover_camera_xhci_irqs()


if __name__ == "__main__":
    unittest.main()
