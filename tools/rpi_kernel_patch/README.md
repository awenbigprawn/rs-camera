# Raspberry Pi RealSense UVC format patch

This directory contains optional Linux kernel patches used in Intel RealSense
experiments:

- `realsense-d16-rw16-uvc.patch`
- `uvcvideo-increase-urbs-16.patch`

The D16/RW16 format patch is not used in the paper. The URB-count patch is an
experimental mitigation and must be treated as an independent experimental
factor rather than part of the baseline kernel.

## Purpose

The RealSense D435 advertises two vendor-specific UVC format GUIDs that are
not recognized by the unmodified Raspberry Pi Linux 6.12 UVC driver:

```text
Unknown video format 00000050-0000-0010-8000-00aa00389b71
Unknown video format 36315752-1a66-a242-9065-d01814a8ef8a
```

The patch adds mappings for these formats:

| UVC format | GUID | V4L2 mapping |
|---|---|---|
| D16 | `00000050-0000-0010-8000-00aa00389b71` | `V4L2_PIX_FMT_Z16` |
| RW16 | `36315752-1a66-a242-9065-d01814a8ef8a` | `V4L2_PIX_FMT_RW16` |

It changes only the Linux UVC/V4L2 format definitions and lookup tables. It
does not modify librealsense.

## When to use it

Use the patch when the kernel must enumerate the D16 and RW16 formats exposed
by the camera. It also removes the corresponding `Unknown video format`
messages during UVC device enumeration.

The patch is not required by the current benchmark profile, which requests
Z16 depth, Y8 infrared, and RGB8 color streams. That profile was verified on
both x86 Ubuntu Linux 6.8 and Raspberry Pi Linux 6.12 without this patch.

The patch was built and tested on Raspberry Pi Linux:

```text
version: 6.12.96
branch:  rpi-6.12.y
commit:  ae161617d7a4552fa91d03626b8d9f3696d17481
```

Other kernel versions may require the patch context to be adjusted.

## Apply the patch

Start from a clean kernel source tree. From the kernel source directory, run:

```sh
PATCH_FILE=/home/safebot/program/rs-camera/tools/rpi_kernel_patch/realsense-d16-rw16-uvc.patch

git apply --check "$PATCH_FILE"
git apply "$PATCH_FILE"
```

Confirm the changes:

```sh
git diff -- \
    drivers/media/common/uvc.c \
    drivers/media/v4l2-core/v4l2-ioctl.c \
    include/linux/usb/uvc.h \
    include/uapi/linux/videodev2.h
```

After applying the patch, rebuild and install the kernel, device trees, and
kernel modules using the same configuration as the unpatched kernel.

## Revert the patch

Before reverting, check that the reverse operation applies cleanly:

```sh
git apply --check --reverse "$PATCH_FILE"
git apply --reverse "$PATCH_FILE"
```

Rebuild and reinstall the kernel and modules after reverting.

## UVC isochronous URB depth experiment

`uvcvideo-increase-urbs-16.patch` changes the Linux UVC driver's fixed
isochronous request pool from 5 URBs to 16 URBs. It does not change USB packet
contents, RealSense formats, or librealsense. A larger in-flight pool gives
the host controller and UVC driver more buffering against delayed URB
resubmission, at the cost of additional kernel memory and potentially more
queued data.

Apply it independently to a clean source tree:

```sh
PATCH_FILE=/home/safebot/program/rs-camera/tools/rpi_kernel_patch/uvcvideo-increase-urbs-16.patch

git apply --check "$PATCH_FILE"
git apply "$PATCH_FILE"
```

This patch was adapted for the Raspberry Pi Linux 6.12.96 source revision
listed above. Build it with a distinct kernel local version so that the
unmodified baseline remains available for rollback and controlled A/B tests.
Verify the effective source value before building:

```sh
grep '^#define UVC_URBS' drivers/media/usb/uvc/uvcvideo.h
```

## Verify the installed kernel

Reconnect the camera and inspect the current boot's kernel log:

```sh
journalctl -k -b --no-pager |
    grep -E 'Unknown video format|RealSense|uvcvideo'
```

With the patch installed, the two format GUID warnings shown above should no
longer appear. Stream functionality should still be validated independently
with the intended resolutions, formats, frame rates, and number of cameras.
