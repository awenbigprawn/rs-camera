#!/bin/sh
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
version=$(sed -n 's/^channel = "\([^"]*\)"/\1/p' "$repo/rust-toolchain.toml")
test -n "$version"
exec rustup run "$version" cargo build --locked --release \
    --manifest-path "${1:-$repo/deps/lime-rtw}/Cargo.toml"
