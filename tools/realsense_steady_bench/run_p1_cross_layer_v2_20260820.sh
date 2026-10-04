#!/bin/sh
set -eu

repo=/home/safebot/program/rs-camera
tool_dir="$repo/tools/realsense_steady_bench"
python="$repo/.venv/bin/python"
runner="$tool_dir/run_steady_campaign.py"
config="$tool_dir/p1_v2_cases_20260820.json"
helper="$tool_dir/.xhci_irq_control_20260810.py"
validator="$tool_dir/validate_p1_v2_cell.py"
build_dir="$repo/build-realsense-steady"
result_root=${RS_P1_RESULT_ROOT:-$tool_dir/results/p1_cross_layer_v2_20260820}
profiles="$result_root/profiles"
monitor_build=/tmp/rs-p1-v2-uvc-worker-monitor
monitor="$monitor_build/uvc_worker_fifo_monitor"
monitor_object="$monitor_build/uvc_worker_fifo_monitor.bpf.o"
state_file=${RS_P1_STATE_FILE:-/home/safebot/.local/state/rs-p1-cross-layer-v2.state}
service=${RS_P1_SERVICE:-rs-p1-cross-layer-v2.service}
p1_trace_mode=${RS_P1_TRACE_MODE:-none}
p1_workload_scope=${RS_P1_WORKLOAD_SCOPE:-all}
p1_profile_source=${RS_P1_PROFILE_SOURCE:-}
boot_config=/boot/firmware/config.txt
cmdline=/boot/firmware/cmdline.txt
standard_cmdline=/boot/firmware/standard-6.12-btf-uvc16/cmdline.txt
rt_cmdline=/boot/firmware/rt-6.12-btf-uvc16/cmdline.txt
current_irq_original=
monitor_pid=

mkdir -p "$(dirname "$state_file")" "$result_root"

restore_runtime_state()
{
    if [ -n "$monitor_pid" ]; then
        sudo -n kill -TERM "$monitor_pid" 2>/dev/null || true
        wait "$monitor_pid" 2>/dev/null || true
        monitor_pid=
    fi
    if [ -n "$current_irq_original" ] && [ -f "$current_irq_original" ]; then
        sudo -n "$python" "$helper" restore \
            --repo-root "$repo" \
            --original-input "$current_irq_original" \
            --output "$(dirname "$current_irq_original")/irq_restored_after_error.json" \
            || true
        current_irq_original=
    fi
}

record_failure()
{
    status=$?
    if [ "$status" -ne 0 ]; then
        restore_runtime_state
        printf 'status=%s\ntime=%s\nstate=%s\n' \
            "$status" "$(date --iso-8601=seconds)" \
            "$(cat "$state_file" 2>/dev/null || true)" > "$result_root/FAILED"
    fi
}
trap record_failure 0
trap 'exit 130' 1 2 15

disable_wifi()
{
    sudo -n systemctl stop wpa_supplicant.service 2>/dev/null || true
    sudo -n ip link set wlan0 down 2>/dev/null || true
}

disable_autosuspend()
{
    printf '%s\n' -1 | sudo -n tee /sys/module/usbcore/parameters/autosuspend >/dev/null
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

record_usb_state()
{
    output=$1
    {
        printf 'usbcore.autosuspend=%s\n' "$(cat /sys/module/usbcore/parameters/autosuspend)"
        for device in /sys/bus/usb/devices/*; do
            [ -f "$device/idVendor" ] || continue
            [ "$(cat "$device/idVendor")" = 8086 ] || continue
            printf '%s product=%s serial=%s speed=%s control=%s\n' \
                "${device##*/}" "$(cat "$device/idProduct")" \
                "$(cat "$device/serial" 2>/dev/null || true)" \
                "$(cat "$device/speed" 2>/dev/null || true)" \
                "$(cat "$device/power/control")"
        done
    } > "$output"
}

ensure_monitor_build()
{
    "$tool_dir/build_uvc_worker_fifo_monitor.sh" "$monitor_build" \
        > "$result_root/uvc_worker_monitor_build.txt"
    test -x "$monitor"
    test -r "$monitor_object"
}

set_boot()
{
    kernel=$1
    threading=$2
    case "$kernel" in
        standard)
            prefix=standard-6.12-btf-uvc16/
            kernel_cmdline=$standard_cmdline
            ;;
        rt)
            prefix=rt-6.12-btf-uvc16/
            kernel_cmdline=$rt_cmdline
            ;;
        *) return 2 ;;
    esac
    sudo -n sed -i "s#^os_prefix=.*#os_prefix=$prefix#" "$boot_config"
    sudo -n sed -i 's/[[:space:]]threadirqs//g' "$kernel_cmdline"
    if [ "$threading" = yes ]; then
        sudo -n sed -i 's/$/ threadirqs/' "$kernel_cmdline"
    fi
    sync
}

restore_original_boot()
{
    sudo -n cp "$result_root/boot_original_config.txt" "$boot_config"
    sudo -n cp "$result_root/boot_original_cmdline.txt" "$cmdline"
    sudo -n cp "$result_root/boot_original_standard_cmdline.txt" "$standard_cmdline"
    sudo -n cp "$result_root/boot_original_rt_cmdline.txt" "$rt_cmdline"
    sync
}

profile_for_case()
{
    case "$1" in
        *stress60) printf '%s\n' "$profiles/stress.csv" ;;
        *) printf '%s\n' "$profiles/representative.csv" ;;
    esac
}

set_noise_args()
{
    case "$1" in
        none) NOISE_ARGUMENTS= ;;
        cpu4)
            NOISE_ARGUMENTS='--cpu-noise-modes
busy_loop
--cpu-noise-workers
4
--cpu-noise-warmup-seconds
2
--cpu-noise-ready-timeout-seconds
15'
            ;;
        memory2000)
            NOISE_ARGUMENTS='--memory-noise-modes
fixed_copy
--memory-noise-workers
4
--memory-noise-buffer-size-mib
64
--memory-noise-copy-chunk-kib
1024
--memory-noise-target-mib-per-second
2000
--memory-noise-warmup-seconds
2
--memory-noise-ready-timeout-seconds
15'
            ;;
        gpu)
            NOISE_ARGUMENTS='--gpu-noise-modes
mobilenet_v2_vulkan
--gpu-noise-warmup-iterations
10
--gpu-noise-ready-timeout-seconds
30'
            ;;
        *) return 2 ;;
    esac
}

uvc_policy_for_sdk()
{
    case "$1" in
        other) printf '%s\n' keep ;;
        rr-rm) printf '%s\n' rr ;;
        fifo-rm|deadline) printf '%s\n' fifo ;;
        *) return 2 ;;
    esac
}

xhci_policy_for_sdk()
{
    case "$1" in
        rr-rm) printf '%s\n' SCHED_RR ;;
        fifo-rm|deadline) printf '%s\n' SCHED_FIFO ;;
        *) return 2 ;;
    esac
}

start_monitor()
{
    policy=$1
    log=$2
    sudo -n "$monitor" \
        --bpf-object "$monitor_object" --policy "$policy" --priority 89 \
        --duration 0 --log "$log" \
        > "$log.stdout" 2> "$log.stderr" &
    monitor_pid=$!
    sleep 1
    kill -0 "$monitor_pid"
}

stop_monitor()
{
    sudo -n kill -TERM "$monitor_pid"
    wait "$monitor_pid"
    monitor_pid=
}

configure_irq_for_cell()
{
    phase=$1
    policy=$2
    output_dir=$3
    current_irq_original=
    case "$phase:$policy" in
        standard-hardirq:other)
            ! grep -qw threadirqs /proc/cmdline
            if ps -eLo comm= | grep -q '^irq/[0-9][0-9]*-xhci'; then
                printf '%s\n' 'unexpected xHCI IRQ thread in hard-IRQ cell' >&2
                return 1
            fi
            printf '%s\n' '{"mode":"hard-irq","schedulable":false}' \
                > "$output_dir/irq_effective_before.json"
            ;;
        rt-threaded:other)
            sudo -n "$python" "$helper" snapshot \
                --repo-root "$repo" --output "$output_dir/irq_effective_before.json"
            ;;
        *:other)
            printf '%s\n' "OTHER is invalid in phase $phase" >&2
            return 1
            ;;
        *)
            irq_policy=$(xhci_policy_for_sdk "$policy")
            current_irq_original="$output_dir/irq_original.json"
            sudo -n "$python" "$helper" configure \
                --repo-root "$repo" --cpus keep \
                --policy "$irq_policy" --priority 90 \
                --original-output "$current_irq_original" \
                --effective-output "$output_dir/irq_effective_before.json"
            ;;
    esac
}

verify_and_restore_irq()
{
    phase=$1
    policy=$2
    output_dir=$3
    case "$phase:$policy" in
        standard-hardirq:other)
            ! grep -qw threadirqs /proc/cmdline
            ;;
        rt-threaded:other)
            sudo -n "$python" "$helper" snapshot \
                --repo-root "$repo" --output "$output_dir/irq_effective_after.json"
            ;;
        *)
            irq_policy=$(xhci_policy_for_sdk "$policy")
            sudo -n "$python" "$helper" verify \
                --repo-root "$repo" --cpus keep \
                --policy "$irq_policy" --priority 90 \
                --output "$output_dir/irq_effective_after.json"
            sudo -n "$python" "$helper" restore \
                --repo-root "$repo" \
                --original-input "$current_irq_original" \
                --output "$output_dir/irq_restored.json"
            current_irq_original=
            ;;
    esac
}

run_probe()
{
    case_id=$1
    policy=$2
    noise=$3
    output_dir=$4
    profile=$(profile_for_case "$case_id")
    set_noise_args "$noise"
    old_ifs=$IFS
    IFS='
'
    # Every item in NOISE_ARGUMENTS is a constant option or value without spaces.
    # shellcheck disable=SC2086
    set -- $NOISE_ARGUMENTS
    IFS=$old_ifs
    case "$p1_trace_mode" in
        none) ;;
        host-latency) set -- "$@" --host-latency-kernel-trace ;;
        *)
            printf 'unknown P1 trace mode: %s\n' "$p1_trace_mode" >&2
            return 2
            ;;
    esac
    "$python" "$runner" \
        --config "$config" --case "$case_id" \
        --policies "$policy" --scheduler-profile "$profile" \
        --nb-runs 1 --measurement-duration-seconds 30 \
        --recover-on-failure full-reset --reset-before-run \
        --max-attempts-per-run 3 --recovery-settle-seconds 0 \
        --build-jobs 3 --build-dir "$build_dir" \
        --backend v4l2 --no-cpu-isolation --no-lime \
        --v4l2-diagnostics-build-only \
        "$@" --results-dir "$output_dir/results"
}

run_cell()
{
    cell_phase=$1
    cell_round=$2
    cell_case_id=$3
    cell_policy=$4
    cell_noise=$5
    cell_relative="$cell_phase/formal/round-$cell_round/$cell_case_id/noise-$cell_noise/policy-$cell_policy"
    cell_output_dir="$result_root/$cell_relative"
    [ ! -f "$cell_output_dir/DONE" ] || return 0
    mkdir -p "$cell_output_dir"

    outer=1
    while [ "$outer" -le 2 ]; do
        if [ -d "$cell_output_dir/results" ]; then
            mv "$cell_output_dir/results" \
                "$cell_output_dir/failed-results-$outer-$(date +%Y%m%d_%H%M%S)"
        fi
        cell_uvc_policy=$(uvc_policy_for_sdk "$cell_policy")
        cell_monitor_log="$cell_output_dir/uvc_worker_monitor-$outer.jsonl"
        configure_irq_for_cell "$cell_phase" "$cell_policy" "$cell_output_dir"
        disable_autosuspend
        start_monitor "$cell_uvc_policy" "$cell_monitor_log"

        run_status=0
        run_probe "$cell_case_id" "$cell_policy" "$cell_noise" "$cell_output_dir" \
            || run_status=$?
        stop_monitor
        irq_status=0
        verify_and_restore_irq "$cell_phase" "$cell_policy" "$cell_output_dir" \
            || irq_status=$?

        validation_status=1
        if [ "$run_status" -eq 0 ] && [ "$irq_status" -eq 0 ]; then
            "$python" "$validator" "$cell_output_dir" "$cell_policy" \
                "$cell_noise" "$cell_uvc_policy" && validation_status=0
        fi
        if [ "$validation_status" -eq 0 ]; then
            record_usb_state "$cell_output_dir/usb_power_after.txt"
            date --iso-8601=seconds > "$cell_output_dir/DONE"
            return 0
        fi
        printf 'outer=%s run=%s irq=%s validation=%s\n' \
            "$outer" "$run_status" "$irq_status" "$validation_status" \
            >> "$cell_output_dir/outer_retry.log"
        restore_runtime_state
        outer=$((outer + 1))
    done
    printf 'cell exhausted retries: %s\n' "$cell_relative" >&2
    return 1
}

run_phase()
{
    matrix_phase=$1
    matrix_policies=$2
    matrix_expected_kernel=$3
    matrix_expected_threading=$4
    [ "$(uname -r)" = "$matrix_expected_kernel" ]
    if [ "$matrix_expected_threading" = yes ]; then
        if [ "$matrix_phase" != rt-threaded ]; then
            grep -qw threadirqs /proc/cmdline
        fi
    else
        ! grep -qw threadirqs /proc/cmdline
    fi
    disable_wifi
    disable_autosuspend
    ensure_monitor_build
    mkdir -p "$result_root/$matrix_phase"
    uname -a > "$result_root/$matrix_phase/uname.txt"
    cat /proc/cmdline > "$result_root/$matrix_phase/cmdline.txt"
    record_usb_state "$result_root/$matrix_phase/usb_power_at_boot.txt"

    round=1
    while [ "$round" -le 3 ]; do
        case "$round" in
            1)
                workloads='p1v2_d435_n2_representative30 p1v2_d435_n2_stress60'
                noises='none cpu4 memory2000 gpu'
                ;;
            2)
                workloads='p1v2_d435_n2_stress60 p1v2_d435_n2_representative30'
                noises='gpu memory2000 cpu4 none'
                ;;
            3)
                workloads='p1v2_d435_n2_representative30 p1v2_d435_n2_stress60'
                noises='memory2000 none gpu cpu4'
                ;;
        esac
        if [ "$p1_workload_scope" = stress ]; then
            workloads='p1v2_d435_n2_stress60'
        elif [ "$p1_workload_scope" = representative ]; then
            workloads='p1v2_d435_n2_representative30'
        elif [ "$p1_workload_scope" != all ]; then
            printf 'unknown P1 workload scope: %s\n' "$p1_workload_scope" >&2
            return 2
        fi
        for case_id in $workloads; do
            for noise in $noises; do
                for policy in $matrix_policies; do
                    run_cell "$matrix_phase" "$round" "$case_id" "$policy" "$noise"
                done
            done
        done
        date --iso-8601=seconds > "$result_root/$matrix_phase/ROUND_$round.COMPLETED"
        round=$((round + 1))
    done
    date --iso-8601=seconds > "$result_root/$matrix_phase/COMPLETED"
}

wait_for_calibration()
{
    if [ -n "$p1_profile_source" ]; then
        mkdir -p "$profiles"
        cp "$p1_profile_source/representative.csv" "$profiles/representative.csv"
        cp "$p1_profile_source/stress.csv" "$profiles/stress.csv"
        return 0
    fi
    while [ ! -f "$result_root/calibration/COMPLETED" ]; do
        if [ -f "$result_root/calibration/FAILED" ]; then
            printf '%s\n' 'P1 calibration failed' >&2
            return 1
        fi
        sleep 10
    done
    test -s "$profiles/representative.csv"
    test -s "$profiles/stress.csv"
}

sudo -n true
state=$(cat "$state_file" 2>/dev/null || printf '%s' prepare)
case "$state" in
    prepare)
        wait_for_calibration
        cp "$boot_config" "$result_root/boot_original_config.txt"
        cp "$cmdline" "$result_root/boot_original_cmdline.txt"
        cp "$standard_cmdline" "$result_root/boot_original_standard_cmdline.txt"
        cp "$rt_cmdline" "$result_root/boot_original_rt_cmdline.txt"
        cp "$config" "$result_root/cases.json"
        git -C "$repo" rev-parse HEAD > "$result_root/git_commit.txt"
        git -C "$repo" status --short > "$result_root/git_status.txt"
        printf '%s\n' \
            'P1 v2 cross-layer matrix, 2026-08-20.' \
            'Two D435 cameras on separate Raspberry Pi 5 xHCI controllers.' \
            'RelWithDebInfo, V4L2+uvcvideo UVC_URBS=16, 1.5 GHz, no affinity or isolation.' \
            'Each cell: 30-frame warm-up, 30-second measurement, three repetitions.' \
            'OTHER/non-RT: hard IRQ plus observe-only UVC eBPF controller.' \
            'RR-RM: xHCI RR90 plus UVC-copy RR89. FIFO-RM: xHCI FIFO90 plus UVC-copy FIFO89.' \
            'Deadline: xHCI FIFO90 plus UVC-copy FIFO89. RT/OTHER retains native IRQ policy.' \
            'P1 records frame metadata and inter-delivery without LiME or the full diagnostic trace.' \
            "Trace mode: $p1_trace_mode; workload scope: $p1_workload_scope." \
            'All cells attach the same UVC eBPF monitor; OTHER uses observe-only mode.' \
            'Noise cells: none, four register-only CPU workers, fixed-copy 2000 MiB/s, or Vulkan MobileNetV2.' \
            > "$result_root/design.txt"
        printf '%s\n' standard-hardirq > "$state_file"
        set_boot standard no
        sudo -n systemctl reboot
        ;;
    standard-hardirq)
        run_phase standard-hardirq other \
            6.12.96-rpi5-standard-btf-uvc16+ no
        printf '%s\n' standard-threaded > "$state_file"
        set_boot standard yes
        sudo -n systemctl reboot
        ;;
    standard-threaded)
        run_phase standard-threaded 'rr-rm fifo-rm deadline' \
            6.12.96-rpi5-standard-btf-uvc16+ yes
        printf '%s\n' rt-threaded > "$state_file"
        set_boot rt no
        sudo -n systemctl reboot
        ;;
    rt-threaded)
        run_phase rt-threaded 'other rr-rm fifo-rm deadline' \
            6.12.96-rpi5-rt-btf-uvc16+ yes
        date --iso-8601=seconds > "$result_root/COMPLETED"
        printf '%s\n' restore > "$state_file"
        restore_original_boot
        sudo -n systemctl reboot
        ;;
    restore)
        sudo -n systemctl disable "$service" || true
        printf '%s\n' done > "$state_file"
        ;;
    done) ;;
    *)
        printf 'unknown P1 state: %s\n' "$state" >&2
        exit 2
        ;;
esac
