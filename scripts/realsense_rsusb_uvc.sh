#!/bin/sh
# Unbind or rebind one verified RealSense USB device for the RSUSB backend.

set -eu

usage() {
    printf '%s\n' \
        "Usage: sudo scripts/realsense_rsusb_uvc.sh {unbind|bind} USB_DEVICE" \
        "" \
        "Example: sudo scripts/realsense_rsusb_uvc.sh unbind 3-1"
}

die() {
    echo "error: $*" >&2
    exit 1
}

[ "$#" -eq 2 ] || {
    usage >&2
    exit 2
}

ACTION=$1
USB_DEVICE=$2

case "$ACTION" in
    unbind|bind)
        ;;
    *)
        die "action must be 'unbind' or 'bind'"
        ;;
esac

case "$USB_DEVICE" in
    *[!A-Za-z0-9._:-]*|'')
        die "invalid USB device name: $USB_DEVICE"
        ;;
esac

[ "$(id -u)" -eq 0 ] || die "run this helper with sudo"

DEVICE_PATH="/sys/bus/usb/devices/$USB_DEVICE"
[ -d "$DEVICE_PATH" ] || die "USB device does not exist: $USB_DEVICE"
[ -r "$DEVICE_PATH/idVendor" ] || die "missing idVendor for $USB_DEVICE"
[ -r "$DEVICE_PATH/manufacturer" ] || die "missing manufacturer for $USB_DEVICE"

VENDOR=$(tr '[:upper:]' '[:lower:]' < "$DEVICE_PATH/idVendor")
MANUFACTURER=$(cat "$DEVICE_PATH/manufacturer")
[ "$VENDOR" = "8086" ] || die "$USB_DEVICE is not an Intel USB device"
case "$MANUFACTURER" in
    *RealSense*)
        ;;
    *)
        die "$USB_DEVICE is not identified as a RealSense device"
        ;;
esac

INTERFACE_COUNT=0
for INTERFACE_PATH in "$DEVICE_PATH":*; do
    [ -d "$INTERFACE_PATH" ] || continue
    INTERFACE=${INTERFACE_PATH##*/}

    if [ "$ACTION" = "unbind" ]; then
        if [ -L "$INTERFACE_PATH/driver" ] &&
            [ "$(basename "$(readlink -f "$INTERFACE_PATH/driver")")" = "uvcvideo" ]
        then
            printf '%s' "$INTERFACE" > /sys/bus/usb/drivers/uvcvideo/unbind
            INTERFACE_COUNT=$((INTERFACE_COUNT + 1))
        fi
    else
        if [ ! -L "$INTERFACE_PATH/driver" ]; then
            printf '%s' "$INTERFACE" > /sys/bus/usb/drivers/uvcvideo/bind
            INTERFACE_COUNT=$((INTERFACE_COUNT + 1))
        fi
    fi
done

printf 'RSUSB_UVC {"action":"%s","usb_device":"%s","interfaces_changed":%s}\n' \
    "$ACTION" "$USB_DEVICE" "$INTERFACE_COUNT"
