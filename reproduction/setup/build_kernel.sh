#!/bin/sh
# Build a fresh isolated tree from the locally available, pinned kernel commit.
set -eu
if [ "$#" -ne 2 ]; then
    echo 'Usage: build_kernel.sh {standard-urb5|standard-urb16|rt-urb16} NEW_OUTPUT_DIRECTORY'
    echo 'Optional KERNEL_GIT, CROSS_COMPILE and BUILD_JOBS environment variables.'
    exit 2
fi
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/../.." && pwd)
variant=$1
case "$variant" in
 standard-urb5) release=6.12.96-rpi5-standard-btf+; boot=artifacts/standard-boot; stage=artifacts/standard-root-stripped;;
 standard-urb16) release=6.12.96-rpi5-standard-btf-uvc16+; boot=artifacts/standard-uvc16-boot; stage=stage-standard-btf-uvc16;;
 rt-urb16) release=6.12.96-rpi5-rt-btf-uvc16+; boot=artifacts/rt-uvc16-boot; stage=stage-rt-btf-uvc16;;
 *) echo 'Unknown kernel variant' >&2; exit 2;;
esac
out=$(realpath -m "$2")
[ ! -e "$out" ] || { echo 'Use a new output directory' >&2; exit 1; }
source_git=${KERNEL_GIT:-$repo/build-rpi-kernel/source}
commit=ae161617d7a4552fa91d03626b8d9f3696d17481
# Verify the exact commit before creating output. No downloads or dirty source reuse.
git -C "$source_git" cat-file -e "$commit^{commit}"
mkdir -p "$out/source" "$out/build" "$out/$boot" "$out/$stage"
git -C "$source_git" archive --format=tar --output="$out/kernel-source.tar" "$commit"
tar -xf "$out/kernel-source.tar" -C "$out/source"
rm "$out/kernel-source.tar"
if [ "$variant" != standard-urb5 ]; then
    git -C "$out/source" apply "$repo/tools/rpi_kernel_patch/uvcvideo-increase-urbs-16.patch"
fi
cp "$here/kernel-configs/$variant.config" "$out/build/.config"
case "$(uname -m)" in aarch64|arm64) cross=${CROSS_COMPILE:-};; *) cross=${CROSS_COMPILE:-aarch64-linux-gnu-};; esac
kernel_make() { make -C "$out/source" O="$out/build" ARCH=arm64 CROSS_COMPILE="$cross" LOCALVERSION=+ "$@"; }
kernel_make olddefconfig
actual=$(kernel_make -s kernelrelease)
[ "$actual" = "$release" ] || { echo "Unexpected release: $actual" >&2; exit 1; }
kernel_make -j"${BUILD_JOBS:-3}" Image modules dtbs
kernel_make INSTALL_MOD_PATH="$out/$stage" INSTALL_MOD_STRIP=1 modules_install
cp "$out/build/arch/arm64/boot/Image" "$out/$boot/vmlinuz"
cp "$out/build/arch/arm64/boot/dts/broadcom/"*.dtb "$out/$boot/"
cp -a "$out/build/arch/arm64/boot/dts/overlays" "$out/$boot/"
cp "$out/build/.config" "$out/$boot/config-$release"
cp "$out/build/System.map" "$out/$boot/System.map-$release"
printf '%s\n' "$commit" > "$out/SOURCE_COMMIT"
printf '%s\n' "Built $release. Install with setup/kernel.py --artifacts $out"
