#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PYTHON=${PYTHON:-$REPO_ROOT/.venv/bin/python}
PERF_BIN=${PERF_BIN:-/usr/lib/linux-tools/6.8.0-138-generic/perf}
RESULTS_DIR=${RESULTS_DIR:-$SCRIPT_DIR/results/capture_residual_pmu_20260818}
PROBE=$REPO_ROOT/build-realsense-steady/realsense_steady_probe
DONE_FILE=$RESULTS_DIR/.campaign-done
PERF_SECONDS=${PERF_SECONDS:-20}
ATTACH_DELAY_SECONDS=${ATTACH_DELAY_SECONDS:-5}

EVENTS=cycles,instructions,L1-dcache-loads,L1-dcache-load-misses,LLC-loads,LLC-load-misses,cache-misses,stalled-cycles-backend

mkdir -p "$RESULTS_DIR/perf"
rm -f "$DONE_FILE"

find_probe_pids()
{
    # Benchkit launches LiME and the probe through sudo.  Scan all root-owned
    # executable links inside one sudo invocation: starting sudo once per PID
    # is slow enough on the Pi to miss a short-lived 30-second probe.
    sudo -n sh -c '
        target=$1
        for proc_dir in /proc/[0-9]*; do
            [ -e "$proc_dir/exe" ] || continue
            executable=$(readlink "$proc_dir/exe" 2>/dev/null || true)
            [ "$executable" = "$target" ] || continue
            basename "$proc_dir"
        done
    ' sh "$PROBE"
}

watch_probes()
{
    seen=" "
    while [ ! -f "$DONE_FILE" ]; do
        for pid in $(find_probe_pids); do
            case "$seen" in
                *" $pid "*) continue ;;
            esac
            seen="$seen$pid "
            (
                sleep "$ATTACH_DELAY_SECONDS"
                [ -d "/proc/$pid/task" ] || exit 0
                tids=$(sudo -n ls "/proc/$pid/task" | sort -n | paste -sd, -)
                [ -n "$tids" ] || exit 0
                output=$RESULTS_DIR/perf/perf-stat-pid-$pid.csv
                metadata=$RESULTS_DIR/perf/perf-stat-pid-$pid.meta
                {
                    printf 'pid=%s\n' "$pid"
                    printf 'tids=%s\n' "$tids"
                    printf 'attach_delay_seconds=%s\n' "$ATTACH_DELAY_SECONDS"
                    printf 'measurement_seconds=%s\n' "$PERF_SECONDS"
                    printf 'events=%s\n' "$EVENTS"
                } >"$metadata"
                sudo -n "$PERF_BIN" stat \
                    --per-thread \
                    -t "$tids" \
                    -x ';' \
                    --output "$output" \
                    -e "$EVENTS" \
                    -- sleep "$PERF_SECONDS" || true
            ) &
        done
        sleep 1
    done
    wait
}

watch_probes &
watcher_pid=$!

campaign_status=0
"$PYTHON" "$SCRIPT_DIR/run_steady_campaign.py" \
    --config "$SCRIPT_DIR/configs/capture_residual_diagnosis_30s.json" \
    --case residual_nested_d455_n1_representative30 \
    --case residual_nested_mixed_n4_representative30 \
    --policies other \
    --nb-runs 3 \
    --recover-on-failure full-reset \
    --max-attempts-per-run 3 \
    --recovery-settle-seconds 0 \
    --no-cpu-isolation \
    --build-jobs 3 \
    --results-dir "$RESULTS_DIR/campaign" || campaign_status=$?

touch "$DONE_FILE"
wait "$watcher_pid"
exit "$campaign_status"
