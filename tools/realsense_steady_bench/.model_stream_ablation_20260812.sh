#!/bin/sh
set -eu

repo=/home/safebot/program/rs-camera
tool_dir="$repo/tools/realsense_steady_bench"
result_root="$tool_dir/results/model_stream_ablation_20260812"
state_file=/home/safebot/.local/state/rs-model-stream-ablation.state
runner="$tool_dir/run_steady_campaign.py"
helper="$tool_dir/.xhci_irq_control_20260810.py"
config="$tool_dir/.model_stream_ablation_cases_20260812.json"
python="$repo/.venv/bin/python"
build_dir="$repo/build-realsense-steady"
boot_config=/boot/firmware/config.txt
cmdline=/boot/firmware/cmdline.txt
standard_cmdline=/boot/firmware/standard-6.12-btf-uvc16/cmdline.txt
rt_cmdline=/boot/firmware/rt-6.12-btf-uvc16/cmdline.txt
service=rs-model-stream-ablation.service
current_original=

mkdir -p "$(dirname "$state_file")" "$result_root"

restore_irq()
{
    if [ -n "$current_original" ] && [ -f "$current_original" ]; then
        sudo -n "$python" "$helper" restore \
            --repo-root "$repo" --original-input "$current_original" \
            --output "$(dirname "$current_original")/irq-restored-after-error.json" \
            || true
        current_original=
    fi
}

record_failure()
{
    status=$?
    if [ "$status" -ne 0 ]; then
        restore_irq
        printf 'status=%s state=%s time=%s\n' "$status" \
            "$(cat "$state_file" 2>/dev/null || printf unknown)" \
            "$(date --iso-8601=seconds)" > "$result_root/FAILED"
    fi
}
trap record_failure 0
trap 'exit 130' 1 2 15

set_standard_boot()
{
    sudo -n sed -i \
        's#^os_prefix=.*#os_prefix=standard-6.12-btf-uvc16/#' "$boot_config"
    if ! grep -qw threadirqs "$standard_cmdline"; then
        sudo -n sed -i '1 s/$/ threadirqs/' "$standard_cmdline"
    fi
    sync
}

set_rt_boot()
{
    sudo -n sed -i 's#^os_prefix=.*#os_prefix=rt-6.12-btf-uvc16/#' "$boot_config"
    sudo -n sed -i 's/ threadirqs//g' "$rt_cmdline"
    sync
}

restore_boot()
{
    sudo -n cp "$result_root/boot-original-config.txt" "$boot_config"
    sudo -n cp "$result_root/boot-original-cmdline.txt" "$cmdline"
    sudo -n cp "$result_root/boot-original-standard-cmdline.txt" \
        "$standard_cmdline"
    sync
}

disable_interference_sources()
{
    sudo -n systemctl stop wpa_supplicant.service 2>/dev/null || true
    sudo -n ip link set wlan0 down 2>/dev/null || true
    printf '%s\n' -1 | sudo -n tee /sys/module/usbcore/parameters/autosuspend \
        >/dev/null
    for device in /sys/bus/usb/devices/*; do
        [ -f "$device/idVendor" ] || continue
        [ "$(cat "$device/idVendor")" = 8086 ] || continue
        case "$(cat "$device/idProduct")" in
            0ad3|0b07|0b5c)
                printf '%s\n' on | sudo -n tee "$device/power/control" >/dev/null
                ;;
        esac
    done
}

record_environment()
{
    output=$1
    mkdir -p "$output"
    uname -a > "$output/uname.txt"
    cat /proc/cmdline > "$output/cmdline.txt"
    lsusb -t > "$output/lsusb-tree.txt"
    git -C "$repo" rev-parse HEAD > "$output/git-commit.txt"
    git -C "$repo" status --short > "$output/git-status.txt"
}

run_cell_once()
{
    kernel=$1
    case_id=$2
    output=$3
    mkdir -p "$output"
    current_original="$output/irq-original.json"
    sudo -n "$python" "$helper" configure \
        --repo-root "$repo" --cpus keep --policy SCHED_OTHER \
        --original-output "$current_original" \
        --effective-output "$output/irq-effective-before.json"
    status=0
    if "$python" "$runner" \
        --config "$config" --case "$case_id" \
        --policies other --nb-runs 3 \
        --measurement-duration-seconds 30 \
        --recover-on-failure full-reset --reset-before-run \
        --max-attempts-per-run 3 --recovery-settle-seconds 0 \
        --build-jobs 3 --build-dir "$build_dir" \
        --backend v4l2 --no-cpu-isolation \
        --v4l2-diagnostics-build-only \
        --results-dir "$output/results"
    then
        status=0
    else
        status=$?
    fi
    irq_status=0
    sudo -n "$python" "$helper" verify \
        --repo-root "$repo" --cpus keep --policy SCHED_OTHER \
        --output "$output/irq-effective-after.json" || irq_status=$?
    sudo -n "$python" "$helper" restore \
        --repo-root "$repo" --original-input "$current_original" \
        --output "$output/irq-restored.json"
    current_original=
    [ "$status" -eq 0 ] && [ "$irq_status" -eq 0 ]
}

run_cell()
{
    kernel=$1
    case_id=$2
    cell="$result_root/$kernel/$case_id"
    [ ! -f "$cell/DONE" ] || return 0
    attempt=1
    while [ "$attempt" -le 2 ]; do
        output="$cell/campaign-attempt-$attempt"
        if run_cell_once "$kernel" "$case_id" "$output"; then
            printf '%s\n' "$attempt" > "$cell/selected-attempt.txt"
            date --iso-8601=seconds > "$cell/DONE"
            return 0
        fi
        restore_irq
        attempt=$((attempt + 1))
    done
    return 1
}

run_kernel()
{
    kernel=$1
    disable_interference_sources
    record_environment "$result_root/$kernel/environment"
    run_cell "$kernel" model_ablation_d435_n1_depth60
    run_cell "$kernel" model_ablation_d435_n1_depth_color60
}

state=$(cat "$state_file" 2>/dev/null || printf '%s' prepare)
case "$state" in
    prepare)
        test "$(uname -r)" = 6.12.96-rpi5-standard-btf-uvc16+
        cp "$boot_config" "$result_root/boot-original-config.txt"
        cp "$cmdline" "$result_root/boot-original-cmdline.txt"
        cp "$standard_cmdline" \
            "$result_root/boot-original-standard-cmdline.txt"
        cp "$config" "$result_root/cases.json"
        printf '%s\n' \
            'Controlled one-D435 60-FPS stream-composition ablation, 2026-08-12.' \
            'Depth and depth+color are measured here; all streams reuse model_camera_scaling_20260812 stress.' \
            'Two 6.12 kernels, SCHED_OTHER userspace/xHCI, three 30-second LiME traces per cell.' \
            > "$result_root/design.txt"
        printf '%s\n' standard > "$state_file"
        set_standard_boot
        sudo -n systemctl reboot
        ;;
    standard)
        test "$(uname -r)" = 6.12.96-rpi5-standard-btf-uvc16+
        grep -qw threadirqs /proc/cmdline
        run_kernel standard
        printf '%s\n' rt > "$state_file"
        set_rt_boot
        sudo -n systemctl reboot
        ;;
    rt)
        test "$(uname -r)" = 6.12.96-rpi5-rt-btf-uvc16+
        run_kernel rt
        date --iso-8601=seconds > "$result_root/completed-at.txt"
        printf '%s\n' restore > "$state_file"
        restore_boot
        sudo -n systemctl reboot
        ;;
    restore)
        sudo -n systemctl disable "$service" || true
        printf '%s\n' done > "$state_file"
        date --iso-8601=seconds > "$result_root/RESTORED"
        ;;
    done)
        ;;
    *)
        printf 'unknown state: %s\n' "$state" >&2
        exit 2
        ;;
esac
