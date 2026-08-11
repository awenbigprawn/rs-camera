#!/bin/sh
set -eu

remote=rpi5-realsense
remote_root=/home/safebot/program/rs-camera/tools/realsense_steady_bench/results/rtns_no_isolation_matrix_20260811
local_root=/home/safebot/program/rs-camera/results/rpi5/rtns_no_isolation_matrix_20260811

mkdir -p "$local_root"
while :; do
    line=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$remote" \
        "cat '$remote_root/stage.ready' 2>/dev/null || true" || true)
    stage=$(printf '%s' "$line" | cut -f1)
    output=$(printf '%s' "$line" | cut -f2)
    if [ -n "$stage" ] && [ -n "$output" ] \
        && [ ! -f "$local_root/$stage.backed_up" ]; then
        mkdir -p "$local_root/$output"
        rsync -a --partial \
            "$remote:$remote_root/$output/" "$local_root/$output/"
        date --iso-8601=seconds > "$local_root/$stage.backed_up"
        ssh -o BatchMode=yes "$remote" \
            "touch '$remote_root/$stage.backed_up'"
    fi
    if [ "$stage" = complete ] || [ "${stage#failed-}" != "$stage" ]; then
        date --iso-8601=seconds > "$local_root/backup_monitor_finished_at.txt"
        exit 0
    fi
    sleep 10
done
