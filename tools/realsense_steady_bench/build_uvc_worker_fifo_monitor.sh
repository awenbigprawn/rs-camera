#!/bin/sh

set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
build_dir=${1:-"$script_dir/build-uvc-worker-monitor"}
clang_bin=${CLANG:-clang}
cc_bin=${CC:-cc}

case $(uname -m) in
    aarch64) target_arch=arm64 ;;
    x86_64) target_arch=x86 ;;
    *)
        echo "Unsupported architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

multiarch=$($cc_bin -dumpmachine)
mkdir -p "$build_dir"

"$clang_bin" \
    -O2 -g -target bpf \
    -D"__TARGET_ARCH_$target_arch" \
    -I"/usr/include/$multiarch" \
    -c "$script_dir/uvc_worker_fifo_monitor.bpf.c" \
    -o "$build_dir/uvc_worker_fifo_monitor.bpf.o"

libbpf_flags=$(pkg-config --cflags --libs libbpf)
# Word splitting is intentional for compiler flags returned by pkg-config.
# shellcheck disable=SC2086
"$cc_bin" -O2 -g -Wall -Wextra -Werror \
    "$script_dir/uvc_worker_fifo_monitor.c" \
    -o "$build_dir/uvc_worker_fifo_monitor" \
    $libbpf_flags

echo "$build_dir/uvc_worker_fifo_monitor"
