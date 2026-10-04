# Dependency versions for the paper artifact

[Pi setup](../reproduction/setup/README.md) · [Two reproduction levels](../reproduction/README.md)

Use Python **3.12** and the recorded source commits. Do not run `git pull` in
submodules or replace the diagnostic SDK with a distribution package.

| Component | Lock | Verification |
| --- | --- | --- |
| Instrumented librealsense 2.58.3, Benchkit, LiME, ncnn | Superproject gitlinks and [sources.json](sources.json) | `--sources` checks gitlinks, commits and clean dependency trees |
| Python runtime, including transitive dependencies | [requirements.txt](../requirements.txt) | `--python` checks versions and Benchkit's editable source location |
| Python build frontend/backend, including editable install support | [python-build.txt](python-build.txt) | `--build-tools` |
| Rust 1.97.1 | [rust-toolchain.toml](../rust-toolchain.toml) | `--rust` |
| Rust crates and Git dependencies | `deps/lime-rtw/Cargo.lock` in the pinned submodule | All repository LiME build wrappers use `cargo build --locked` |
| Pi system build/runtime libraries | [ubuntu-24.04-arm64.txt](ubuntu-24.04-arm64.txt) | `--system` |
| Linux 6.12.96 | Commit `ae161617d7a4552fa91d03626b8d9f3696d17481`, saved configs and URB patch | [Kernel builder](../reproduction/setup/build_kernel.sh) checks the commit and release |

Install the Python-only environment on a laptop without sudo:

```sh
git submodule update --init --recursive
sh scripts/install_python.sh
.venv/bin/python scripts/check_dependencies.py --sources --python --build-tools
```

`install_python.sh` installs exact versions with dependency resolution disabled,
then checks package metadata. It installs the pinned build backend explicitly
and disables isolated builds, so build dependencies cannot float. Use a fresh
venv for review (`VENV_DIR=/path/to/venv`); an existing venv may contain unrelated
packages that this installer deliberately does not remove. Python runtime pins
match the measured Pi environment; build-only pins have been verified by a clean
install. No Matplotlib dependency is needed: paper figures use native TikZ.

`reproduction/setup/install.sh` enables `--locked-system`, which installs exact
`package=version` pairs on **Ubuntu 24.04 ARM64 only**, including GCC/G++ 13.3,
libusb, libbpf, libelf, OpenSSL, udev and the Vulkan/Mesa stack. The generic
`scripts/install_dependencies_ubuntu.sh` keeps distro-selected system packages
unless this option is requested; that mode is for development, not an identical
paper platform. The lock records direct build packages and experiment-facing
runtime libraries, not every package in an immutable OS image. Matching packages
must remain available from the configured Ubuntu mirror or a saved package cache.
APT fails if a pinned version is unavailable; the installer never substitutes a
newer version or forces downgrades. Do not hold OS security updates globally.

Optional PDF rendering uses Ubuntu 24.04's TeX Live 2023 packages:

```sh
sudo apt-get install texlive-latex-extra=2023.20240207-1 texlive-pictures=2023.20240207-1
```

These package versions were checked against the Pi's APT index. TeX rendering
has not been exercised on the Pi; CSV and native TeX generation have. Fonts and
other OS/TeX transitive packages are not a byte-for-byte PDF environment lock.

After Pi setup, audit all recorded dependencies:

```sh
.venv/bin/python scripts/check_dependencies.py --sources --python --build-tools --rust --system
```

To update dependencies intentionally, change the relevant lock and gitlink
together, rerun clean-install checks and the archived-data reproduction, and
record a new artifact revision. `scripts/update_deps.sh` restores the pinned
submodules; it does not advance them to branch heads.
