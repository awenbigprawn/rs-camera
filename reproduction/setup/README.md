# Step-by-step Raspberry Pi setup

[Back to the reproduction guide](../README.md) · [E1](../e1_workers/README.md) ·
[E2](../e2_uvc_pool/README.md) · [E3](../e3_kernel_path/README.md) ·
[E4](../e4_scheduling/README.md)

For automatic complete reproduction, finish steps 1–5, install the optional
PDF dependencies below, then run `./reproduce.sh full` from the repository root.
It owns discovery/preparation, kernel switching and automatic reboot/resume.
For manual execution, complete steps 1–6 once, then use step 7 between stages. Unless marked
**laptop**, commands run on the **Pi**, from the `rs-camera` repository root.
Do not run an acquisition campaign while configuring the machine.

## 1. Install the OS and connect over Ethernet

The prepared evaluation machine is a Raspberry Pi 5 with 8 GB RAM and
64-bit Ubuntu 24.04 (currently 24.04.4). Use that OS series to match the
GCC/G++ 13.3.0 toolchain. The experiment replaces its stock kernel with the
three matched Linux 6.12.96 builds below. Raspberry Pi OS has not been
validated by this workflow.

1. On the laptop, write the Ubuntu Server **24.04 ARM64 Raspberry Pi** image
   to the Pi's boot media. Follow the official
   [Ubuntu installation guide](https://ubuntu.com/tutorials/how-to-install-ubuntu-on-your-raspberry-pi)
   for flashing and first login; select the 24.04 image explicitly.
2. Connect Ethernet, cooling and a suitable Pi power supply. Complete first
   boot and create a regular user with sudo access. Use a monitor/keyboard if
   SSH has not been configured in the image.
3. At the Pi console, enable SSH and obtain its Ethernet address:

   ```sh
   sudo apt-get update
   sudo apt-get install -y openssh-server git
   sudo systemctl enable --now ssh
   ip -br address show eth0
   ```

4. On the laptop, connect using your actual account and address:

   ```sh
   ssh YOUR_USER@PI_ETHERNET_ADDRESS
   ```

5. On the Pi, verify the starting environment:

   ```sh
   cat /etc/os-release
   uname -m
   free -h
   df -h / /boot/firmware
   ```

Expected architecture: `aarch64`. Keep enough free storage for sources, builds
and traces: the archived paper data is approximately 3.9 GB and one existing
kernel build tree is approximately 8.5 GB. These are observed sizes, not upper
bounds. Build kernels on the laptop or use a separate disk if Pi storage is
limited. Keep the working stock boot configuration until the custom kernels
are installed.

## 2. Obtain the artifact and dependencies

Clone the repository and record the evaluation revision. All capture adapters,
analysis functions and plot code are tracked here; a separate paper source
checkout is not required. Public submodules use HTTPS and are pinned by Git.

```sh
git clone --recurse-submodules https://github.com/awenbigprawn/rs-camera.git
cd rs-camera
git rev-parse HEAD
python3 scripts/check_dependencies.py --sources
```

If an evaluation revision is supplied, check out that revision and run
`sh scripts/update_deps.sh` before installation. The exact source, Python, Rust
and Pi system package versions are described in
[dependency locks](../../dependencies/README.md).

The raw-data archive is a **separate input**, not downloaded by Git. Obtain it
from the supplied evaluation package, unpack it at
`paperwriting/paper_used_raw_data/` (or use `--input PATH`), and verify its
`SHA256SUMS`. Kernel source or matched prebuilt artifacts are also separate;
step 5 documents their exact commit and build/install commands.

Do not substitute an unmodified upstream SDK solely because its version is
2.58.3: the pinned custom commit contains the paper's diagnostic markers.

## 3. Install and build the tools

Run as the regular user, with RealSense cameras disconnected for the initial
udev rule installation:

```sh
sh reproduction/setup/install.sh
```

This delegates dependency and build work to
[`scripts/install_dependencies_ubuntu.sh`](../../scripts/install_dependencies_ubuntu.sh),
using V4L2 and RelWithDebInfo, with diagnostic markers in the steady build.
The installer enables the exact Ubuntu 24.04 ARM64 package lock; unavailable
versions cause an error instead of silently choosing newer packages. It builds the steady probe, startup probe,
pthread tracer, LiME, GPU workload and UVC eBPF monitor, installs the existing
SDK udev rules and adds the tracing/kernel-build tools. It does not install or
switch a kernel. The SDK submodule must be the patched artifact version.

Check the build:

```sh
gcc -dumpfullversion
g++ -dumpfullversion
test -x build-realsense-steady/d435_sensor_probe
test -x build-realsense-steady/realsense_steady_probe
test -x build-realsense-thread-trace/d435_sensor_probe
test -f build-realsense-thread-trace/libtrace_pthreads.so
test -x deps/lime-rtw/target/release/lime-rtw
test -f deps/ncnn/benchmark/models/mobilenet_v2.param
trace-cmd --version
```

For default one-command PDF generation (both `raw` and `full`), also install:

```sh
sudo apt-get install -y texlive-latex-extra=2023.20240207-1 texlive-pictures=2023.20240207-1
```

Alternatively pass `--no-pdf` to the root script to retain CSV and TeX output
without PDF compilation.

Both compiler versions must be `13.3.0` for the paper preflight. If the distro
supplies another version, restore the evaluated toolchain and rebuild; do not
edit the preflight to disguise the difference. GPU interference requires the
Pi's V3D Vulkan driver and the supplied ncnn model definition. Kernel boot
configurations retain the existing `vc4-kms-v3d-pi5` overlay. See the
[steady benchmark guide](../../tools/realsense_steady_bench/README.md) for the
underlying build and workload details.

## 4. Build and install the matched kernels

Three variants are required:

| Variant | Expected `uname -r` | UVC URBs | Preemption |
| --- | --- | --- | --- |
| `standard-urb5` | `6.12.96-rpi5-standard-btf+` | 5 | PREEMPT |
| `standard-urb16` | `6.12.96-rpi5-standard-btf-uvc16+` | 16 | PREEMPT |
| `rt-urb16` | `6.12.96-rpi5-rt-btf-uvc16+` | 16 | PREEMPT_RT |

All use HZ=1000 and BTF. Full reference configurations are in
[`kernel-configs/`](kernel-configs/). The URB16 variants reuse
[the existing URB-count patch](../../tools/rpi_kernel_patch/uvcvideo-increase-urbs-16.patch).
The D16/RW16 format patch is not part of this experiment.

### Build from the pinned source

Skip building if the artifact supplies matching boot files and modules.
Otherwise obtain the pinned Raspberry Pi Linux source (on Pi or Ubuntu laptop):

```sh
mkdir -p build-rpi-kernel
git init build-rpi-kernel/source
git -C build-rpi-kernel/source fetch --depth 1 \
  https://github.com/raspberrypi/linux.git ae161617d7a4552fa91d03626b8d9f3696d17481
# The build script archives this commit directly; no checkout is required.
sh reproduction/setup/build_kernel.sh standard-urb5 build-rpi-kernel/review-standard-urb5
sh reproduction/setup/build_kernel.sh standard-urb16 build-rpi-kernel/review-standard-urb16
sh reproduction/setup/build_kernel.sh rt-urb16 build-rpi-kernel/review-rt-urb16
```

Each output must be new. The script uses the preserved config and only the
variant's required patch, and checks the resulting kernel release. On an x86
Ubuntu laptop first install `gcc-aarch64-linux-gnu make bison flex bc libssl-dev
libelf-dev dwarves`; the script chooses that cross compiler automatically.
If building on the laptop, copy the generated `artifacts/` and `stage-*` trees
for each variant to the same output paths on the Pi. Only those trees are
needed for installation; the source/build trees can stay on the laptop.

### Install on the Pi

For each newly built variant:

```sh
python3 reproduction/setup/kernel.py install standard-urb5 \
  --artifacts build-rpi-kernel/review-standard-urb5
sudo python3 reproduction/setup/kernel.py install standard-urb5 \
  --artifacts build-rpi-kernel/review-standard-urb5 --apply
sudo python3 reproduction/setup/kernel.py install standard-urb16 \
  --artifacts build-rpi-kernel/review-standard-urb16 --apply
sudo python3 reproduction/setup/kernel.py install rt-urb16 \
  --artifacts build-rpi-kernel/review-rt-urb16 --apply
```

For the original artifact layout use `--artifacts build-rpi-kernel` instead.
The installer verifies kernel configuration, copies images, DTBs, overlays and
matching modules, and creates an initramfs. It refuses to overwrite installed
images/modules; use `select` for an already prepared machine. Installation and
selection are dry-runs without `--apply`, and neither reboots automatically.

Boot RT URB16 for the first capture group:

```sh
sudo python3 reproduction/setup/kernel.py select rt-urb16 --apply
sudo reboot
```

Reconnect, return to the repository root, and verify:

```sh
uname -r
cat /proc/cmdline
test -r /sys/kernel/btf/vmlinux
```

Expected release: `6.12.96-rpi5-rt-btf-uvc16+`.

## 5. Connect and discover the cameras

1. Connect two D435 cameras to different Pi xHCI controllers, using externally
   powered USB3 hubs; attach at least one D455/D455F. A second D455 is optional.
   Reconnect cameras after installing the udev rules so permissions take effect.
2. Inspect the USB tree and generate camera configuration:

   ```sh
   lsusb -t
   python3 reproduction/setup/discover.py
   # Equivalent main entry point: python3 reproduction/reproduce.py discover
   ```

3. Inspect the saved files:

   ```sh
   python3 -m json.tool reproduction/setup/config.json
   python3 -m json.tool reproduction/setup/inventory.json
   ```

| File | Contents |
| --- | --- |
| `config.json` | Supported camera models, SDK serials, xHCI IRQ numbers/labels and experiment-role selections; consumed by all four experiments |
| `inventory.json` | All SDK-enumerated cameras, firmware, product ID, physical port, supported stream profiles, USB speed/topology/controller, IRQ mapping, timestamp and discovery issues |
| `config.previous-TIMESTAMP.json` | Prior config, created if successful discovery changes it |
| `config.example.json` | Schema reference for manual inspection |
| `config.pi5.json` | Historical evaluated-machine example; do not use its serials for another machine |

Discovery reuses the benchmark's USB/IRQ discovery code and the existing probe's
new `--list-devices` mode. It joins SDK and USB records by physical ancestry,
**not** by USB descriptor serial: those serials differ on these cameras. It
queries supported profiles without starting acquisition or changing firmware.
Supported profiles describe capability, not proof of simultaneous streaming;
the capture tools validate the actual workload.

On first discovery, roles are selected deterministically by SDK serial:
a D435 pair on distinct controllers, one D455/D455F, and single-camera roles.
Later discovery retains role aliases and selections. To intentionally choose a
new set, use `--new-selection`; to choose another attached supported camera,
edit only the role names in `selection` before preparing a new campaign.

If discovery fails, read `inventory.json` and the error. No valid existing
config is overwritten. USB2 connections require correcting the cable/hub/port;
missing SDK devices usually require reconnecting after udev installation.
A laptop may enumerate cameras but cannot validate the Pi-specific xHCI layout.
Complete configuration on the Pi. Unknown models remain in the inventory and
are not substituted for the paper's D435/D455 roles.

## 6. Check and prepare the campaign

Keep wired/local access and disable Wi-Fi:

```sh
sh reproduction/setup/network.sh status
sh reproduction/setup/network.sh disable-wifi
python3 reproduction/setup/discover.py --check
python3 reproduction/reproduce.py plan
python3 reproduction/reproduce.py prepare
sudo -v
python3 reproduction/e1_workers/run.py check e1
```

Expected: camera configuration matches live discovery, inspectable adapters
under `reproduction/runs/entrypoints`, `prepared.json` with source/config
hashes, and `Preflight passed` for E1 on RT URB16. `prepare` does not collect
measurements. Use the same `--output PATH` for every capture stage if you choose
a nondefault campaign location. The historical source adapters require the
artifact's source versions and fail if a source boundary changes.

Acquisition uses noninteractive sudo internally. Ensure it remains available
for the stage under the machine's administrative policy; `sudo -v` may expire
before a long stage finishes. A password prompt or permission error is not a
valid measurement. Do not run another camera campaign concurrently.

Proceed to [E1](../e1_workers/README.md), then follow the
[main guide's kernel-grouped ordering](../README.md#3-run-the-four-experiments).

## 7. Switch kernels between stages

Use the mode required by the next experiment:

```sh
# Non-RT URB16 with threaded xHCI: E1 ablation, E3, E4 calibration/coordinated
sudo python3 reproduction/setup/kernel.py select standard-urb16 --threadirqs --apply
# Non-RT URB5 with hard IRQ: E2 URB5
sudo python3 reproduction/setup/kernel.py select standard-urb5 --apply
# Non-RT URB16 with hard IRQ: startup, E2 URB16, E4 OTHER, supplements
sudo python3 reproduction/setup/kernel.py select standard-urb16 --apply
# RT URB16: E1, E2 calibration, E4 RT
sudo python3 reproduction/setup/kernel.py select rt-urb16 --apply
```

**Execute only the one selection for the next group**, then `sudo reboot`.
After reconnecting, run `uname -r`, `cat /proc/cmdline`, `discover.py --check`,
`network.sh status` and the experiment's `run.py check STAGE` before capture.
RT has native threaded interrupts; the non-RT modes require the declared
presence/absence of `threadirqs`. Keep physical USB wiring fixed across stages.
If IRQs or selected cameras change, stop and review the config/adapters;
`prepare` rejects changed configurations in an existing campaign directory.
Do not silently combine stages from different camera selections. The automatic
`full` controller handles IRQ-only changes separately, keeping physical camera
roles fixed and preserving each boot/stage configuration for audit.

Each selection saves `/boot/firmware/reproduction-backup-TIMESTAMP/`.
To restore, copy its `config.txt` to `/boot/firmware/config.txt` and its
`cmdline.txt` to `/boot/firmware/PREFIX/cmdline.txt`, using PREFIX from the
backup's `prefix.txt`, then reboot. Keep console or boot-media access for
recovering a failed boot. To restore the Wi-Fi link, use
`sh reproduction/setup/network.sh enable-wifi`; the existing network manager
handles reconnection. No script replaces routes, netplan, or SSH configuration.

## Validation scope

Automatic discovery and repeat checks were exercised on the prepared Pi with
four connected cameras. Unit tests cover physical-path matching, different SDK
and USB serials, stable role selection and topology failures. Kernel and
network scripts have offline checks; the complete instructions have not yet
been validated as a fresh installation on a blank Pi. Retain installation
logs and the exact source manifest when performing that validation.
