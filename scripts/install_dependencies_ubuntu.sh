#!/bin/sh
#
# Prepare an Ubuntu machine for the rs-camera librealsense timing benchmarks.
# Run this script as a regular user; it invokes sudo only for apt.

set -eu

SCRIPT_DIR=$(CDPATH='' cd -P "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd -P "$SCRIPT_DIR/.." && pwd)

BUILD_PROJECT=0
SYSTEM_ONLY=0
SKIP_APT_UPDATE=0
RSUSB_BACKEND=OFF
GPU_NOISE=0
BUILD_JOBS="${BUILD_JOBS:-}"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build-realsense-thread-trace}"
VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"

usage() {
    cat <<'EOF'
Usage: scripts/install_dependencies_ubuntu.sh [OPTIONS]

Install dependencies for librealsense, LiME/eBPF, Benchkit, and the RealSense
startup and steady-state benchmarks.

Options:
  --build                 Build LiME, d435_sensor_probe, and the pthread tracer.
  --build-dir PATH        CMake build directory used by --build.
  --rsusb-backend         Build vendored librealsense with its libusb backend.
  --gpu-noise             Install Vulkan packages and build MobileNetV2 GPU noise.
  --build-jobs N          Parallel build jobs (default: online CPUs minus one).
  --system-only           Install system packages and Rust; skip project setup.
  --skip-apt-update       Do not run apt-get update.
  -h, --help              Show this help.

Environment:
  BUILD_DIR               Default CMake build directory.
  VENV_DIR                Default Python virtual environment directory.
  BUILD_JOBS              Default parallel build jobs.

Run this script without sudo. It requests sudo only for apt package operations.
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

apt_sources_include_suite() {
    suite=$1

    for source_file in \
        /etc/apt/sources.list \
        /etc/apt/sources.list.d/*.list \
        /etc/apt/sources.list.d/*.sources
    do
        [ -f "$source_file" ] || continue
        if awk -v suite="$suite" '
            /^[[:space:]]*#/ {
                next
            }
            /^[[:space:]]*deb(-src)?[[:space:]]/ ||
            /^[[:space:]]*Suites:[[:space:]]/ {
                for (field = 1; field <= NF; field++) {
                    if ($field == suite) {
                        found = 1
                    }
                }
            }
            END {
                exit(found ? 0 : 1)
            }
        ' "$source_file"
        then
            return 0
        fi
    done

    return 1
}

ensure_ubuntu_updates_source() {
    [ "${ID:-}" = "ubuntu" ] || return 0

    ubuntu_codename=${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}
    [ -n "$ubuntu_codename" ] ||
        die "cannot determine the Ubuntu codename from /etc/os-release"
    updates_suite="${ubuntu_codename}-updates"

    if apt_sources_include_suite "$updates_suite"; then
        return 0
    fi

    if [ "$SKIP_APT_UPDATE" -eq 1 ]; then
        die "APT suite '$updates_suite' is missing; rerun without --skip-apt-update so it can be configured"
    fi

    case "$(dpkg --print-architecture)" in
        amd64|i386)
            ubuntu_archive_uri="http://archive.ubuntu.com/ubuntu/"
            ;;
        *)
            ubuntu_archive_uri="http://ports.ubuntu.com/ubuntu-ports/"
            ;;
    esac

    updates_source="/etc/apt/sources.list.d/rs-camera-${updates_suite}.sources"
    updates_source_tmp=$(mktemp)
    trap 'rm -f "$updates_source_tmp"' 0 HUP INT TERM
    {
        printf '%s\n' \
            "Types: deb" \
            "URIs: $ubuntu_archive_uri" \
            "Suites: $updates_suite" \
            "Components: main restricted universe multiverse" \
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg"
    } > "$updates_source_tmp"

    echo "APT suite '$updates_suite' is missing; adding $updates_source"
    sudo install -m 0644 "$updates_source_tmp" "$updates_source"
    rm -f "$updates_source_tmp"
    trap - 0 HUP INT TERM
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --build)
            BUILD_PROJECT=1
            shift
            ;;
        --build-dir)
            [ "$#" -ge 2 ] || die "missing value for --build-dir"
            BUILD_DIR=$2
            shift 2
            ;;
        --rsusb-backend)
            RSUSB_BACKEND=ON
            shift
            ;;
        --gpu-noise)
            GPU_NOISE=1
            shift
            ;;
        --build-jobs)
            [ "$#" -ge 2 ] || die "missing value for --build-jobs"
            BUILD_JOBS=$2
            shift 2
            ;;
        --system-only)
            SYSTEM_ONLY=1
            shift
            ;;
        --skip-apt-update)
            SKIP_APT_UPDATE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

if [ -z "$BUILD_JOBS" ]; then
    online_cpus=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')
    if [ "$online_cpus" -gt 1 ]; then
        BUILD_JOBS=$((online_cpus - 1))
    else
        BUILD_JOBS=1
    fi
fi
case "$BUILD_JOBS" in
    *[!0-9]*|0) die "--build-jobs must be a positive integer" ;;
esac

if [ "$SYSTEM_ONLY" -eq 1 ] && [ "$BUILD_PROJECT" -eq 1 ]; then
    die "--system-only and --build cannot be used together"
fi

if [ "$(id -u)" -eq 0 ]; then
    die "run this script as a regular user, not with sudo"
fi

[ -r /etc/os-release ] || die "/etc/os-release is missing"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
    ubuntu|debian)
        ;;
    *)
        die "unsupported distribution '${ID:-unknown}'; this installer requires Ubuntu or Debian"
        ;;
esac

command -v sudo >/dev/null 2>&1 || die "sudo is required"
ensure_ubuntu_updates_source

APT_PACKAGES="
build-essential
ca-certificates
clang
cmake
curl
git
libbpf-dev
libelf-dev
libssl-dev
libudev-dev
libusb-1.0-0-dev
llvm
ninja-build
pkg-config
python3
python3-pip
python3-venv
rustup
usbutils
util-linux
v4l-utils
zlib1g-dev
"

if [ "$GPU_NOISE" -eq 1 ]; then
    APT_PACKAGES="$APT_PACKAGES
libvulkan-dev
mesa-vulkan-drivers
vulkan-tools
"
fi

if [ "$SKIP_APT_UPDATE" -eq 0 ]; then
    sudo apt-get update
fi
# Package names cannot contain whitespace, so intentional word splitting turns
# this newline-separated list into apt-get arguments.
# shellcheck disable=SC2086
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y $APT_PACKAGES

# Ubuntu 24.04 offers rustc/cargo 1.75, which is too old for Cargo.lock v4.
# rustup installs a current toolchain for the invoking user on every supported
# architecture instead of mixing the benchmark with an old distribution Cargo.
rustup set profile minimal
rustup toolchain install stable
rustup default stable
rustup component add rustfmt --toolchain stable
export PATH="$HOME/.cargo/bin:$PATH"

command -v cargo >/dev/null 2>&1 || die "cargo is unavailable after rustup setup"
command -v rustc >/dev/null 2>&1 || die "rustc is unavailable after rustup setup"

echo "System dependency setup complete."
echo "  OS:       ${PRETTY_NAME:-$ID}"
echo "  Arch:     $(uname -m)"
echo "  Kernel:   $(uname -r)"
echo "  CMake:    $(cmake --version | head -n 1)"
echo "  Compiler: $(cc --version | head -n 1)"
echo "  Rust:     $(rustc --version)"
echo "  Cargo:    $(cargo --version)"

if [ -r /sys/kernel/btf/vmlinux ]; then
    echo "  BTF:      /sys/kernel/btf/vmlinux"
else
    echo "warning: /sys/kernel/btf/vmlinux is missing; LiME CO-RE tracing may not work" >&2
fi

if [ "$SYSTEM_ONLY" -eq 1 ]; then
    exit 0
fi

[ -d "$REPO_ROOT/.git" ] || die "$REPO_ROOT is not a Git worktree"
git -C "$REPO_ROOT" submodule update --init --recursive

if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install -e "$REPO_ROOT/deps/benchkit" numpy

echo "Project Python environment is ready: $VENV_DIR"

if [ "$BUILD_PROJECT" -eq 1 ]; then
    cargo build \
        --release \
        --manifest-path "$REPO_ROOT/deps/lime-rtw/Cargo.toml"

    cmake \
        -S "$REPO_ROOT" \
        -B "$BUILD_DIR" \
        -DFORCE_RSUSB_BACKEND="$RSUSB_BACKEND" \
        -DRS_CAMERA_BUILD_GPU_NOISE="$(if [ "$GPU_NOISE" -eq 1 ]; then printf ON; else printf OFF; fi)" \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo
    BUILD_TARGETS="d435_sensor_probe"
    if [ "$GPU_NOISE" -eq 1 ]; then
        BUILD_TARGETS="$BUILD_TARGETS realsense_gpu_noise"
    fi
    # Intentional word splitting supplies one CMake target per list item.
    # shellcheck disable=SC2086
    cmake \
        --build "$BUILD_DIR" \
        --target $BUILD_TARGETS \
        --parallel "$BUILD_JOBS"

    cc \
        -shared \
        -fPIC \
        -g \
        -O2 \
        -fno-omit-frame-pointer \
        -Wall \
        -Wextra \
        -o "$BUILD_DIR/libtrace_pthreads.so" \
        "$REPO_ROOT/tools/realsense_thread_trace/trace_pthreads.c" \
        -ldl \
        -pthread

    echo "Build complete."
    echo "  LiME:   $REPO_ROOT/deps/lime-rtw/target/release/lime-rtw"
    echo "  Probe:  $BUILD_DIR/d435_sensor_probe"
    echo "  Tracer: $BUILD_DIR/libtrace_pthreads.so"
    if [ "$GPU_NOISE" -eq 1 ]; then
        echo "  GPU noise: $BUILD_DIR/realsense_gpu_noise"
    fi
else
    echo
    echo "Dependencies are installed. Build everything with:"
    echo "  $REPO_ROOT/scripts/install_dependencies_ubuntu.sh --skip-apt-update --build"
    echo "Add --gpu-noise to install Vulkan support and build the selected GPU workload."
fi
