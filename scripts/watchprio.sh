#!/bin/sh
set -eu

PATTERN="${1:-${RS_WATCHPRIO_PATTERN:-getframe|d435-probe|d435_sensor|smallest_test|realsense2}}"

watch -n0.1 "ps -eL -o pid,tid,comm,cls,pri,rtprio,psr | grep -E \"$PATTERN\" | grep -v grep | sort -f -k3"
