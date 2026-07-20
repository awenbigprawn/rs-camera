#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
build_dir="${RS_THREAD_TRACE_BUILD_DIR:-${repo_root}/build-realsense-thread-trace}"

cmake \
    -S "${repo_root}" \
    -B "${build_dir}" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    -DCMAKE_C_FLAGS_RELWITHDEBINFO="-O2 -g -DNDEBUG -fno-omit-frame-pointer" \
    -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="-O2 -g -DNDEBUG -fno-omit-frame-pointer"

cmake --build "${build_dir}" --target realsense_thread_lifecycle_probe --parallel "$(nproc)"

echo "Built: ${build_dir}/realsense_thread_lifecycle_probe"
