#!/bin/sh
set -eu

repo=/home/safebot/program/rs-camera
tool_dir="$repo/tools/realsense_steady_bench"
result_root="$tool_dir/results/rtns_no_isolation_matrix_20260811"
state_file=/home/safebot/.local/state/rs-rtns-no-isolation-matrix.state
helper="$tool_dir/.xhci_irq_control_20260810.py"
validator="$tool_dir/.validate_rtns_cell_20260811.py"
runner="$tool_dir/run_steady_campaign.py"
config="$tool_dir/.rtns_no_isolation_cases_20260811.json"
python="$repo/.venv/bin/python"
build_dir="$repo/build-realsense-steady"
source_profiles="$tool_dir/results/xhci_policy_factorial_20260810/model_profiles"
profiles="$result_root/model_profiles"
boot_config=/boot/firmware/config.txt
cmdline=/boot/firmware/cmdline.txt
standard_cmdline=/boot/firmware/standard-6.12-btf-uvc16/cmdline.txt
rt_cmdline=/boot/firmware/rt-6.12-btf-uvc16/cmdline.txt
service=rs-rtns-no-isolation-matrix.service
current_original=

mkdir -p "$(dirname "$state_file")" "$result_root"

restore_current_irq_state()
{
    if [ -n "$current_original" ] && [ -f "$current_original" ]; then
        sudo "$python" "$helper" restore \
            --repo-root "$repo" \
            --original-input "$current_original" \
            --output "$(dirname "$current_original")/irq_restored_after_error.json" \
            || true
        current_original=
    fi
}

record_failure()
{
    status=$?
    if [ "$status" -ne 0 ]; then
        restore_current_irq_state
        failure_stage="failed-$(date +%Y%m%d-%H%M%S)"
        printf '%s\n' "failed status=$status time=$(date --iso-8601=seconds)" \
            > "$result_root/FAILED"
        printf '%s\t%s\n' "$failure_stage" . > "$result_root/stage.ready"
    fi
}
trap record_failure 0
trap 'exit 130' 1 2 15

disable_wifi()
{
    sudo systemctl stop wpa_supplicant.service 2>/dev/null || true
    sudo ip link set wlan0 down 2>/dev/null || true
}

disable_usb_autosuspend()
{
    printf '%s\n' -1 | sudo tee /sys/module/usbcore/parameters/autosuspend \
        >/dev/null
    for device in /sys/bus/usb/devices/*; do
        [ -f "$device/idVendor" ] || continue
        [ "$(cat "$device/idVendor")" = 8086 ] || continue
        case "$(cat "$device/idProduct")" in
            0ad3|0b07|0b5c)
                printf '%s\n' on | sudo tee "$device/power/control" >/dev/null
                ;;
        esac
    done
}

record_usb_power_state()
{
    output=$1
    mkdir -p "$(dirname "$output")"
    {
        printf 'usbcore.autosuspend=%s\n' \
            "$(cat /sys/module/usbcore/parameters/autosuspend)"
        for device in /sys/bus/usb/devices/*; do
            [ -f "$device/idVendor" ] || continue
            [ "$(cat "$device/idVendor")" = 8086 ] || continue
            printf '%s product=%s serial=%s speed=%s control=%s\n' \
                "${device##*/}" \
                "$(cat "$device/idProduct")" \
                "$(cat "$device/serial" 2>/dev/null || true)" \
                "$(cat "$device/speed" 2>/dev/null || true)" \
                "$(cat "$device/power/control")"
        done
    } > "$output"
}

checkpoint()
{
    stage=$1
    output=$2
    printf '%s\t%s\n' "$stage" "$output" > "$result_root/stage.ready.tmp"
    mv "$result_root/stage.ready.tmp" "$result_root/stage.ready"
    while [ ! -f "$result_root/$stage.backed_up" ]; do
        sleep 10
    done
    if [ "$output" != . ]; then
        sudo find "$result_root/$output" -type d -name lime_trace \
            -prune -exec rm -rf -- {} +
        sudo find "$result_root/$output" -type f -name frame_events.csv -delete
    fi
    sync
}

set_boot()
{
    kernel=$1
    if [ "$kernel" = standard ]; then
        prefix=standard-6.12-btf-uvc16/
        kernel_cmdline=$standard_cmdline
        need_threadirqs=yes
    else
        prefix=rt-6.12-btf-uvc16/
        kernel_cmdline=$rt_cmdline
        need_threadirqs=no
    fi
    sudo sed -i "s#^os_prefix=.*#os_prefix=$prefix#" "$boot_config"
    sudo sed -i 's/ threadirqs//g' "$kernel_cmdline"
    if [ "$need_threadirqs" = yes ]; then
        sudo sed -i 's/$/ threadirqs/' "$kernel_cmdline"
    fi
    sync
}

restore_original_boot()
{
    sudo cp "$result_root/boot_original_config.txt" "$boot_config"
    sudo cp "$result_root/boot_original_cmdline.txt" "$cmdline"
    sudo cp "$result_root/boot_original_standard_cmdline.txt" "$standard_cmdline"
    sudo cp "$result_root/boot_original_rt_cmdline.txt" "$rt_cmdline"
    sync
}

irq_policy_for_userspace()
{
    policy=$1
    case "$policy" in
        other) printf '%s\n' SCHED_OTHER ;;
        rr-rm) printf '%s\n' SCHED_RR ;;
        fifo-rm|deadline) printf '%s\n' SCHED_FIFO ;;
        *) return 2 ;;
    esac
}

configure_irq()
{
    policy=$1
    original=$2
    effective=$3
    irq_policy=$(irq_policy_for_userspace "$policy")
    if [ "$irq_policy" = SCHED_OTHER ]; then
        sudo "$python" "$helper" configure \
            --repo-root "$repo" --cpus keep \
            --policy SCHED_OTHER \
            --original-output "$original" \
            --effective-output "$effective"
    else
        sudo "$python" "$helper" configure \
            --repo-root "$repo" --cpus keep \
            --policy "$irq_policy" --priority 90 \
            --original-output "$original" \
            --effective-output "$effective"
    fi
}

verify_irq()
{
    policy=$1
    output=$2
    irq_policy=$(irq_policy_for_userspace "$policy")
    if [ "$irq_policy" = SCHED_OTHER ]; then
        sudo "$python" "$helper" verify \
            --repo-root "$repo" --cpus keep \
            --policy SCHED_OTHER --output "$output"
    else
        sudo "$python" "$helper" verify \
            --repo-root "$repo" --cpus keep \
            --policy "$irq_policy" --priority 90 --output "$output"
    fi
}

profile_for_case()
{
    case "$1" in
        *stress60) printf '%s\n' "$profiles/stress-60fps.csv" ;;
        *) printf '%s\n' "$profiles/representative-30fps.csv" ;;
    esac
}

set_noise_args()
{
    noise=$1
    case "$noise" in
        none)
            set --
            ;;
        cpu4)
            set -- \
                --cpu-noise-modes busy_loop \
                --cpu-noise-workers 4 \
                --cpu-noise-cpu-affinity 0-3
            ;;
        memory2000)
            set -- \
                --memory-noise-modes fixed_copy \
                --memory-noise-workers 4 \
                --memory-noise-buffer-size-mib 64 \
                --memory-noise-copy-chunk-kib 1024 \
                --memory-noise-target-mib-per-second 2000 \
                --memory-noise-cpu-affinity 0-3
            ;;
        gpu)
            set -- \
                --gpu-noise-modes mobilenet_v2_vulkan \
                --gpu-noise-cpu-affinity 0-3
            ;;
        *) return 2 ;;
    esac
    NOISE_ARGUMENTS=$(printf '%s\n' "$@")
}

verify_build_identity()
{
    output_dir=$1
    identity="$output_dir/build_identity.sha256"
    reference="$result_root/build_identity.sha256"
    find "$build_dir" -maxdepth 2 -type f \
        \( -name realsense_steady_probe -o -name 'librealsense2.so.2.58.3' \) \
        -exec sha256sum {} + | sort > "$identity"
    test "$(wc -l < "$identity")" -eq 2
    if [ -f "$reference" ]; then
        cmp "$reference" "$identity"
    else
        cp "$identity" "$reference"
    fi
    grep -qx 'CMAKE_BUILD_TYPE:STRING=RelWithDebInfo' \
        "$build_dir/CMakeCache.txt"
    grep -qx 'CXX_FLAGS = -O2 -g -DNDEBUG' \
        "$build_dir/CMakeFiles/realsense_steady_probe.dir/flags.make"
}

run_probe()
{
    case_id=$1
    policy=$2
    noise=$3
    duration=$4
    output_dir=$5
    profile=$(profile_for_case "$case_id")
    set_noise_args "$noise"
    # NOISE_ARGUMENTS contains one shell-safe option or value per line. All
    # values are constants declared above and contain no whitespace.
    old_ifs=$IFS
    IFS='
'
    # shellcheck disable=SC2086
    set -- $NOISE_ARGUMENTS
    IFS=$old_ifs
    "$python" "$runner" \
        --config "$config" \
        --case "$case_id" \
        --policies "$policy" \
        --scheduler-profile "$profile" \
        --nb-runs 1 \
        --measurement-duration-seconds "$duration" \
        --recover-on-failure full-reset \
        --reset-before-run \
        --max-attempts-per-run 3 \
        --recovery-settle-seconds 0 \
        --build-jobs 3 \
        --build-dir "$build_dir" \
        --backend v4l2 \
        --no-cpu-isolation \
        --no-lime \
        --v4l2-diagnostics-build-only \
        "$@" \
        --results-dir "$output_dir/results"
}

run_trial()
{
    kernel=$1
    round=$2
    case_id=$3
    policy=$4
    noise=$5
    kind=$6
    duration=$7
    output="$kernel/$kind/round-$round/$case_id/noise-$noise/policy-$policy"
    output_dir="$result_root/$output"
    if [ -f "$output_dir/DONE" ]; then
        return 0
    fi
    mkdir -p "$output_dir"

    logical_attempt=1
    while [ "$logical_attempt" -le 2 ]; do
        if [ -d "$output_dir/results" ]; then
            mv "$output_dir/results" \
                "$output_dir/failed-results-$logical_attempt-$(date +%Y%m%d_%H%M%S)"
        fi
        current_original="$output_dir/irq_original-$logical_attempt.json"
        configure_irq "$policy" "$current_original" \
            "$output_dir/irq_effective_before-$logical_attempt.json"
        disable_usb_autosuspend

        run_status=0
        if ! run_probe "$case_id" "$policy" "$noise" "$duration" "$output_dir"; then
            run_status=$?
            [ "$run_status" -ne 0 ] || run_status=1
        fi
        irq_status=0
        verify_irq "$policy" \
            "$output_dir/irq_effective_after-$logical_attempt.json" \
            || irq_status=$?
        sudo "$python" "$helper" restore \
            --repo-root "$repo" \
            --original-input "$current_original" \
            --output "$output_dir/irq_restored-$logical_attempt.json"
        current_original=

        validation_status=0
        if [ "$run_status" -eq 0 ] && [ "$irq_status" -eq 0 ]; then
            "$python" "$validator" "$output_dir" "$policy" "$noise" \
                || validation_status=$?
        else
            validation_status=1
        fi
        if [ "$validation_status" -eq 0 ]; then
            verify_build_identity "$output_dir"
            record_usb_power_state "$output_dir/usb_power_after.txt"
            date --iso-8601=seconds > "$output_dir/DONE"
            return 0
        fi
        printf '%s\n' \
            "logical_attempt=$logical_attempt run_status=$run_status irq_status=$irq_status validation_status=$validation_status" \
            >> "$output_dir/outer_retry.log"
        logical_attempt=$((logical_attempt + 1))
    done
    printf '%s\n' "cell exhausted outer retries: $output" >&2
    return 1
}

run_preflight()
{
    kernel=$1
    run_trial "$kernel" 0 main_d435_n2_representative30 rr-rm cpu4 \
        preflight 10
    checkpoint "$kernel-preflight" "$kernel/preflight"
}

run_formal_matrix()
{
    kernel=$1
    round=1
    while [ "$round" -le 3 ]; do
        case "$round" in
            1)
                workloads='main_d435_n2_representative30 main_d435_n2_stress60'
                noises='none cpu4 memory2000 gpu'
                policies='other rr-rm fifo-rm deadline'
                ;;
            2)
                workloads='main_d435_n2_stress60 main_d435_n2_representative30'
                noises='gpu memory2000 cpu4 none'
                policies='deadline fifo-rm rr-rm other'
                ;;
            3)
                workloads='main_d435_n2_representative30 main_d435_n2_stress60'
                noises='memory2000 none gpu cpu4'
                policies='fifo-rm other deadline rr-rm'
                ;;
        esac
        for case_id in $workloads; do
            for noise in $noises; do
                block="$kernel/formal/round-$round/$case_id/noise-$noise"
                for policy in $policies; do
                    run_trial "$kernel" "$round" "$case_id" "$policy" \
                        "$noise" formal 30
                done
                stage="$kernel-formal-r${round}-${case_id}-${noise}"
                checkpoint "$stage" "$block"
            done
        done
        round=$((round + 1))
    done
}

run_scaling_extension()
{
    kernel=$1
    round=1
    while [ "$round" -le 3 ]; do
        case "$round" in
            1) cases='scaling_d435_n1_representative30 scaling_mixed_n4_representative30 scaling_d435_n1_stress60 scaling_d455_n1_stress60 scaling_d455_n2_stress60' ;;
            2) cases='scaling_d455_n2_stress60 scaling_d455_n1_stress60 scaling_d435_n1_stress60 scaling_mixed_n4_representative30 scaling_d435_n1_representative30' ;;
            3) cases='scaling_mixed_n4_representative30 scaling_d435_n1_stress60 scaling_d455_n2_stress60 scaling_d435_n1_representative30 scaling_d455_n1_stress60' ;;
        esac
        for case_id in $cases; do
            run_trial "$kernel" "$round" "$case_id" other none scaling 30
        done
        checkpoint "$kernel-scaling-r$round" "$kernel/scaling/round-$round"
        round=$((round + 1))
    done
}

run_kernel()
{
    kernel=$1
    expected=$2
    test "$(uname -r)" = "$expected"
    if [ "$kernel" = standard ]; then
        grep -qw threadirqs /proc/cmdline
    fi
    disable_wifi
    disable_usb_autosuspend
    mkdir -p "$result_root/$kernel"
    record_usb_power_state "$result_root/$kernel/usb_power_at_boot.txt"
    uname -a > "$result_root/$kernel/uname.txt"
    cat /proc/cmdline > "$result_root/$kernel/cmdline.txt"
    sudo "$python" "$helper" snapshot \
        --repo-root "$repo" \
        --output "$result_root/$kernel/irq_baseline.json"
    run_preflight "$kernel"
    run_formal_matrix "$kernel"
    run_scaling_extension "$kernel"
    date --iso-8601=seconds > "$result_root/$kernel/COMPLETED"
}

state=$(cat "$state_file" 2>/dev/null || printf '%s' prepare)
case "$state" in
    prepare)
        cp "$boot_config" "$result_root/boot_original_config.txt"
        cp "$cmdline" "$result_root/boot_original_cmdline.txt"
        cp "$standard_cmdline" "$result_root/boot_original_standard_cmdline.txt"
        cp "$rt_cmdline" "$result_root/boot_original_rt_cmdline.txt"
        cp -a "$source_profiles" "$profiles"
        cp "$config" "$result_root/cases.json"
        git -C "$repo" rev-parse HEAD > "$result_root/git_commit.txt"
        git -C "$repo" status --short > "$result_root/git_status.txt"
        printf '%s\n' \
            'RTNS no-isolation formal matrix, 2026-08-11.' \
            'RelWithDebInfo (-O2 -g -DNDEBUG), V4L2 + uvcvideo UVC_URBS=16.' \
            'CPU frequency 1.5 GHz; page cache dropped before each logical run.' \
            'USB autosuspend disabled globally and per connected RealSense device.' \
            'All four CPUs available: no CPU or IRQ isolation and no taskset for the probe.' \
            'Standard kernel uses threadirqs only so xHCI policy is controllable.' \
            'User/IRQ policies: OTHER/OTHER; RR-RM/RR90; FIFO-RM/FIFO90; Deadline/FIFO90.' \
            'Noises are separate: none, four register-only workers, fixed-copy 2000 MiB/s, MobileNetV2 Vulkan.' \
            'Main matrix: two D435, two workloads, four policies, four noises, three 30 s repetitions, two kernels.' \
            'Scaling extension: representative D435 n1 and mixed n4; stress D435 n1 and D455 n1/n2; OTHER, no noise, three repetitions, two kernels.' \
            'Two powered USB3 hubs, one per xHCI controller, at most two cameras per hub.' \
            > "$result_root/design.txt"
        printf '%s\n' standard > "$state_file"
        set_boot standard
        sudo systemctl reboot
        ;;
    standard)
        run_kernel standard 6.12.96-rpi5-standard-btf-uvc16+
        printf '%s\n' rt > "$state_file"
        set_boot rt
        sudo systemctl reboot
        ;;
    rt)
        run_kernel rt 6.12.96-rpi5-rt-btf-uvc16+
        date --iso-8601=seconds > "$result_root/completed_at.txt"
        checkpoint complete .
        printf '%s\n' restore > "$state_file"
        restore_original_boot
        sudo systemctl reboot
        ;;
    restore)
        sudo systemctl disable "$service" || true
        sudo systemctl stop rs-rtns-sudo-expiry.timer 2>/dev/null || true
        sudo rm -f /etc/sudoers.d/90-rs-camera-benchmark
        printf '%s\n' "done" > "$state_file"
        ;;
    done)
        ;;
    *)
        printf '%s\n' "unknown continuation state: $state" >&2
        exit 2
        ;;
esac
