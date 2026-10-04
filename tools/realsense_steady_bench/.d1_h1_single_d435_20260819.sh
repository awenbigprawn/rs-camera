#!/bin/sh
set -eu

repo=/home/safebot/program/rs-camera
tool_dir="$repo/tools/realsense_steady_bench"
result_root="$tool_dir/results/d1_h1_single_d435_20260819"
state_file=/home/safebot/.local/state/rs-d1-h1-single-d435.state
runner="$tool_dir/run_steady_campaign.py"
generator="$tool_dir/generate_deadline_profile.py"
config="$tool_dir/.d1_h1_single_d435_20260819.json"
python="$repo/.venv/bin/python"
build_dir="$repo/build-realsense-steady"
boot_config=/boot/firmware/config.txt
service=rs-d1-h1-single-d435.service
serial=948122073863
current_irq_mode=

mkdir -p "$(dirname "$state_file")" "$result_root"

record_failure()
{
    status=$?
    if [ "$status" -ne 0 ]; then
        {
            printf 'status=%s\n' "$status"
            printf 'time=%s\n' "$(date --iso-8601=seconds)"
            printf 'state=%s\n' "$(cat "$state_file" 2>/dev/null || printf unknown)"
            printf 'kernel=%s\n' "$(uname -r)"
            printf 'cmdline=%s\n' "$(cat /proc/cmdline)"
        } > "$result_root/FAILED"
    fi
}
trap record_failure 0
trap 'exit 130' 1 2 15

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

xhci_tids()
{
    ps -eLo tid=,comm= | awk '$2 ~ /^irq\/[0-9]+-xhci/ {print $1}'
}

record_irq_state()
{
    output=$1
    mkdir -p "$(dirname "$output")"
    {
        date --iso-8601=seconds
        uname -a
        cat /proc/cmdline
        printf '%s\n' '--- /proc/interrupts ---'
        cat /proc/interrupts
        printf '%s\n' '--- xHCI IRQ threads ---'
        ps -eLo tid,cls,rtprio,pri,psr,comm,args | grep -E 'irq/[0-9]+-xhci' || true
        for tid in $(xhci_tids); do
            chrt -p "$tid" || true
        done
    } > "$output"
}

set_threaded_irq_policy()
{
    mode=$1
    tids=$(xhci_tids)
    [ -n "$tids" ] || {
        printf '%s\n' 'No threaded xHCI IRQ workers were found.' >&2
        return 1
    }
    for tid in $tids; do
        case "$mode" in
            other) sudo -n chrt -o -p 0 "$tid" ;;
            fifo50) sudo -n chrt -f -p 50 "$tid" ;;
            fifo90) sudo -n chrt -f -p 90 "$tid" ;;
            *) return 2 ;;
        esac
    done
    current_irq_mode=$mode
}

verify_irq_configuration()
{
    mode=$1
    tids=$(xhci_tids)
    if [ "$mode" = hardirq ]; then
        [ -z "$tids" ] || {
            printf 'Expected hard IRQ service, found threaded xHCI TIDs: %s\n' "$tids" >&2
            return 1
        }
        return 0
    fi
    [ -n "$tids" ] || return 1
    for tid in $tids; do
        state=$(chrt -p "$tid")
        case "$mode" in
            other)
                printf '%s\n' "$state" | grep -q 'SCHED_OTHER'
                ;;
            fifo50)
                printf '%s\n' "$state" | grep -q 'SCHED_FIFO'
                printf '%s\n' "$state" | grep -q 'priority: 50'
                ;;
            fifo90)
                printf '%s\n' "$state" | grep -q 'SCHED_FIFO'
                printf '%s\n' "$state" | grep -q 'priority: 90'
                ;;
        esac
    done
}

record_environment()
{
    output=$1
    expected_urbs=$2
    irq_mode=$3
    mkdir -p "$output"
    uname -a > "$output/uname.txt"
    cat /proc/cmdline > "$output/cmdline.txt"
    lsusb -t > "$output/lsusb-tree.txt"
    lsusb > "$output/lsusb.txt"
    git -C "$repo" rev-parse HEAD > "$output/git-commit.txt"
    git -C "$repo" status --short > "$output/git-status.txt"
    printf '%s\n' "$expected_urbs" > "$output/expected-uvc-urbs.txt"
    printf '%s\n' "$irq_mode" > "$output/expected-irq-mode.txt"
    modinfo -F filename uvcvideo > "$output/uvcvideo-module-path.txt"
    module=$(cat "$output/uvcvideo-module-path.txt")
    sha256sum "$module" > "$output/uvcvideo-module.sha256"
    cat /sys/module/usbcore/parameters/autosuspend > "$output/usbcore-autosuspend.txt"
    for device in /sys/bus/usb/devices/*; do
        [ -f "$device/idVendor" ] || continue
        [ "$(cat "$device/idVendor")" = 8086 ] || continue
        printf '%s product=%s serial=%s speed=%s control=%s\n' \
            "${device##*/}" \
            "$(cat "$device/idProduct")" \
            "$(cat "$device/serial" 2>/dev/null || true)" \
            "$(cat "$device/speed" 2>/dev/null || true)" \
            "$(cat "$device/power/control")"
    done > "$output/realsense-usb-power.txt"
    record_irq_state "$output/irq-state.txt"
}

set_boot()
{
    prefix=$1
    threaded=$2
    kernel_cmdline="/boot/firmware/$prefix/cmdline.txt"
    [ -f "$kernel_cmdline" ]
    sudo -n sed -i "s#^os_prefix=.*#os_prefix=$prefix/#" "$boot_config"
    sudo -n sed -i 's/[[:space:]]threadirqs//g' "$kernel_cmdline"
    if [ "$threaded" = yes ]; then
        sudo -n sed -i 's/$/ threadirqs/' "$kernel_cmdline"
    fi
    sync
}

restore_original_boot()
{
    sudo -n cp "$result_root/boot-original/config.txt" "$boot_config"
    for prefix in standard-6.12-btf standard-6.12-btf-uvc16 rt-6.12-btf-uvc16; do
        sudo -n cp "$result_root/boot-original/$prefix-cmdline.txt" \
            "/boot/firmware/$prefix/cmdline.txt"
    done
    sync
}

profile_for_workload()
{
    case "$1" in
        representative) printf '%s\n' "$result_root/profiles/representative.csv" ;;
        stress) printf '%s\n' "$result_root/profiles/stress.csv" ;;
        *) return 2 ;;
    esac
}

case_for_workload()
{
    case "$1" in
        representative) printf '%s\n' d1_h1_d435_representative30 ;;
        stress) printf '%s\n' d1_h1_d435_stress60 ;;
        *) return 2 ;;
    esac
}

selected_attempt_dir()
{
    root=$1
    selection=$(find "$root" -type f -name selected_attempt.txt | head -n 1)
    [ -n "$selection" ]
    logical=$(dirname "$selection")
    attempt=$(cat "$selection")
    printf '%s/attempt-%s\n' "$logical" "$attempt"
}

verify_build()
{
    library=$(find "$build_dir" -maxdepth 2 -type f \
        -name 'librealsense2.so.2.58.3' | head -n 1)
    [ -n "$library" ]
    grep -qx 'CMAKE_BUILD_TYPE:STRING=RelWithDebInfo' "$build_dir/CMakeCache.txt"
    grep -q -- '-O2' "$build_dir/CMakeFiles/realsense_steady_probe.dir/flags.make"
    grep -q -- '-DNDEBUG' "$build_dir/CMakeFiles/realsense_steady_probe.dir/flags.make"
    sha256sum "$build_dir/realsense_steady_probe" \
        "$library" | sort > "$result_root/build-current.sha256"
    if [ -f "$result_root/build-reference.sha256" ]; then
        cmp "$result_root/build-reference.sha256" "$result_root/build-current.sha256"
    else
        cp "$result_root/build-current.sha256" "$result_root/build-reference.sha256"
    fi
}

calibrate_profiles()
{
    mkdir -p "$result_root/calibration" "$result_root/profiles"
    set_threaded_irq_policy fifo90
    for workload in representative stress; do
        case_id=$(case_for_workload "$workload")
        output="$result_root/calibration/$workload"
        "$python" "$runner" \
            --config "$config" --case "$case_id" \
            --policies other --nb-runs 1 \
            --measurement-duration-seconds 30 \
            --serial "$serial" \
            --recover-on-failure full-reset --reset-before-run \
            --max-attempts-per-run 3 --recovery-settle-seconds 0 \
            --build-jobs 3 --build-dir "$build_dir" \
            --backend v4l2 --no-cpu-isolation \
            --v4l2-diagnostics-build-only \
            --results-dir "$output"
        verify_build
        trace=$(selected_attempt_dir "$output")
        "$python" "$generator" \
            --trace-run "$trace" \
            --output "$(profile_for_workload "$workload")" \
            > "$result_root/profiles/$workload-generator.json"
    done
    set_threaded_irq_policy fifo50
    date --iso-8601=seconds > "$result_root/calibration/COMPLETED"
}

validate_campaign()
{
    output=$1
    "$python" - "$output" <<'PY'
import csv
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
csvs = sorted(root.glob("benchmark_*.csv"))
if len(csvs) != 1:
    raise SystemExit(f"expected one benchmark CSV below {root}, found {len(csvs)}")
lines = [
    line for line in csvs[0].read_text(encoding="utf-8").splitlines()
    if line and not line.startswith("#")
]
rows = list(csv.DictReader(lines, delimiter=";"))
if len(rows) != 3:
    raise SystemExit(f"expected three repetitions, found {len(rows)}")
summary = []
for row in rows:
    item = {
        "rep": int(row.get("rep") or 0),
        "success": row.get("success", "").lower() == "true",
        "eventual_success": row.get("eventual_success", "").lower() == "true",
        "steady_worker_policy_effective": row.get("steady_worker_policy_effective", ""),
        "cpu_noise_ready": row.get("cpu_noise_ready", "").lower() == "true",
        "deliveries": int(row.get("deliveries") or 0),
        "duplicate_frames": int(row.get("duplicate_frames") or 0),
        "sequence_gaps": int(row.get("sequence_gaps") or 0),
        "timeouts": int(row.get("timeouts") or 0),
    }
    summary.append(item)
    if not item["success"] or not item["eventual_success"]:
        raise SystemExit(f"unsuccessful benchmark repetition: {item}")
    if item["steady_worker_policy_effective"] != "SCHED_FIFO":
        raise SystemExit(f"FIFO-RM was not effective: {item}")
    if not item["cpu_noise_ready"]:
        raise SystemExit(f"CPU noise did not report ready: {item}")
(root / "cell-validation.json").write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
}

run_cell()
{
    label=$1
    expected_urbs=$2
    irq_mode=$3
    cell="$result_root/formal/$label"
    [ ! -f "$cell/DONE" ] || return 0
    mkdir -p "$cell"
    disable_autosuspend
    verify_irq_configuration "$irq_mode"
    record_environment "$cell/environment-before" "$expected_urbs" "$irq_mode"
    for workload in representative stress; do
        case_id=$(case_for_workload "$workload")
        output="$cell/$workload"
        [ ! -f "$output/WORKLOAD_DONE" ] || continue
        "$python" "$runner" \
            --config "$config" --case "$case_id" \
            --policies fifo-rm \
            --scheduler-profile "$(profile_for_workload "$workload")" \
            --nb-runs 3 --measurement-duration-seconds 30 \
            --serial "$serial" \
            --recover-on-failure full-reset --reset-before-run \
            --max-attempts-per-run 3 --recovery-settle-seconds 0 \
            --cpu-noise-modes busy_loop --cpu-noise-workers 4 \
            --cpu-noise-warmup-seconds 2 \
            --cpu-noise-ready-timeout-seconds 15 \
            --build-jobs 3 --build-dir "$build_dir" \
            --backend v4l2 --no-cpu-isolation --no-lime \
            --v4l2-diagnostics-build-only \
            --results-dir "$output"
        verify_build
        validate_campaign "$output"
        verify_irq_configuration "$irq_mode"
        date --iso-8601=seconds > "$output/WORKLOAD_DONE"
    done
    record_environment "$cell/environment-after" "$expected_urbs" "$irq_mode"
    date --iso-8601=seconds > "$cell/DONE"
}

run_hardirq_state()
{
    label=$1
    expected_kernel=$2
    expected_urbs=$3
    test "$(uname -r)" = "$expected_kernel"
    ! grep -qw threadirqs /proc/cmdline
    verify_irq_configuration hardirq
    run_cell "$label" "$expected_urbs" hardirq
}

state=$(cat "$state_file" 2>/dev/null || printf '%s' prepare)
case "$state" in
    prepare)
        mkdir -p "$result_root/boot-original"
        cp "$boot_config" "$result_root/boot-original/config.txt"
        for prefix in standard-6.12-btf standard-6.12-btf-uvc16 rt-6.12-btf-uvc16; do
            cp "/boot/firmware/$prefix/cmdline.txt" \
                "$result_root/boot-original/$prefix-cmdline.txt"
        done
        cp "$config" "$result_root/cases.json"
        printf '%s\n' \
            'Single active D435 (librealsense serial 948122073863).' \
            'RelWithDebInfo (-O2 -g -DNDEBUG), V4L2 backend, CPU fixed at 1500 MHz by the campaign.' \
            'No CPU isolation or workload affinity. Four SCHED_OTHER register-only CPU workers start after 30 warm-up frames.' \
            'Userspace acquisition workers use SCHED_FIFO with rate-monotonic priorities up to 80.' \
            'Two workloads, three 30-second repetitions per system state.' \
            'D1 compares UVC_URBS=5 and 16 on non-RT without threadirqs.' \
            'H1 fixes UVC_URBS=16 and compares hard IRQ, non-RT threaded OTHER, RT FIFO50, and RT FIFO90 xHCI service.' \
            > "$result_root/design.txt"
        test "$(uname -r)" = 6.12.96-rpi5-rt-btf-uvc16+
        disable_autosuspend
        calibrate_profiles
        printf '%s\n' std5-hard > "$state_file"
        set_boot standard-6.12-btf no
        sudo -n systemctl reboot
        ;;
    std5-hard)
        run_hardirq_state d1-uvc5-standard-hardirq \
            6.12.96-rpi5-standard-btf+ 5
        printf '%s\n' std16-hard > "$state_file"
        set_boot standard-6.12-btf-uvc16 no
        sudo -n systemctl reboot
        ;;
    std16-hard)
        run_hardirq_state d1-h1-uvc16-standard-hardirq \
            6.12.96-rpi5-standard-btf-uvc16+ 16
        printf '%s\n' std16-thread > "$state_file"
        set_boot standard-6.12-btf-uvc16 yes
        sudo -n systemctl reboot
        ;;
    std16-thread)
        test "$(uname -r)" = 6.12.96-rpi5-standard-btf-uvc16+
        grep -qw threadirqs /proc/cmdline
        set_threaded_irq_policy other
        run_cell h1-uvc16-standard-threadirq-other 16 other
        printf '%s\n' rt > "$state_file"
        set_boot rt-6.12-btf-uvc16 no
        sudo -n systemctl reboot
        ;;
    rt)
        test "$(uname -r)" = 6.12.96-rpi5-rt-btf-uvc16+
        verify_irq_configuration fifo50
        run_cell h1-uvc16-rt-threadirq-fifo50 16 fifo50
        set_threaded_irq_policy fifo90
        run_cell h1-uvc16-rt-threadirq-fifo90 16 fifo90
        set_threaded_irq_policy fifo50
        date --iso-8601=seconds > "$result_root/COMPLETED"
        printf '%s\n' restore > "$state_file"
        restore_original_boot
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
        printf 'Unknown state: %s\n' "$state" >&2
        exit 2
        ;;
esac
