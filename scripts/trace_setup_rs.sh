#!/bin/sh
set -eu

# trace_setup_rs.sh
#
# Purpose:
#   Trace rs-camera executables with scheduler, timer, and optional userspace
#   trace_marker context while keeping output manageable.
#
# Usage:
#   sudo ./scripts/trace_setup_rs.sh -- ./build/d435_sensor_probe --frames 300
#   sudo ./scripts/trace_setup_rs.sh -- ./build/smallest_test
#
# Output:
#   - snap_<cmd>_<timestamp>.txt : full trace text
#   - key_<cmd>_<timestamp>.txt  : compact windows around matching markers

: "${RS_TRACE_BUF_KB:=262144}"
: "${RS_TRACE_EVENT_PID_AUTO:=1}"
: "${RS_TRACE_WIN_BEFORE:=1200}"
: "${RS_TRACE_WIN_AFTER:=600}"
: "${RS_TRACE_MARKER_GREP:=tracing_mark_write: RS_LONG}"
: "${RS_TRACE_IRQ:=0}"
: "${RS_TRACE_SCHEDSTAT_EXTRA:=0}"

die() { echo "ERROR: $*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
}

tracing_dir() {
  if [ -d /sys/kernel/tracing ]; then
    echo /sys/kernel/tracing
  elif [ -d /sys/kernel/debug/tracing ]; then
    echo /sys/kernel/debug/tracing
  else
    die "cannot find tracefs (/sys/kernel/tracing or /sys/kernel/debug/tracing)"
  fi
}

enable_event_if_exists() {
  tr_dir="$1"
  group="$2"
  event="$3"
  path="$tr_dir/events/$group/$event/enable"
  [ -e "$path" ] || return 0
  echo 1 > "$path" || true
}

disable_all_events() {
  tr_dir="$1"
  if [ -e "$tr_dir/events/enable" ]; then
    echo 0 > "$tr_dir/events/enable" || true
  else
    find "$tr_dir/events" -name enable -type f -print0 | xargs -0 -I{} sh -c "echo 0 > \"{}\" 2>/dev/null || true"
  fi
}

clear_filters() {
  tr_dir="$1"
  find "$tr_dir/events" -name filter -type f -print0 | xargs -0 -I{} sh -c "echo 0 > \"{}\" 2>/dev/null || true"
}

set_clock() {
  tr_dir="$1"
  [ -e "$tr_dir/trace_clock" ] || return 0
  if grep -q "mono_raw" "$tr_dir/trace_clock" 2>/dev/null; then
    echo mono_raw > "$tr_dir/trace_clock" || true
  else
    echo mono > "$tr_dir/trace_clock" || true
  fi
}

set_buffer_kb() {
  tr_dir="$1"
  kb="$2"
  [ -e "$tr_dir/buffer_size_kb" ] || return 0
  echo "$kb" > "$tr_dir/buffer_size_kb" || true
}

now_stamp() { date +"%Y%m%d_%H%M%S"; }

cmd_basename() {
  cmd="$1"
  cmd="${cmd##*/}"
  echo "$cmd" | tr -cs "[:alnum:]_." "_"
}

update_set_event_pid_loop() {
  tr_dir="$1"
  app_pid="$2"
  while kill -0 "$app_pid" 2>/dev/null; do
    if [ -d "/proc/$app_pid/task" ]; then
      pids="0 $(ls -1 "/proc/$app_pid/task" 2>/dev/null | paste -sd " " -)"
      echo "$pids" > "$tr_dir/set_event_pid" 2>/dev/null || true
    fi
    sleep 0.2
  done
}

extract_key_windows() {
  snap="$1"
  key="$2"
  before="$3"
  after="$4"
  : > "$key"

  lines="$(grep -n "$RS_TRACE_MARKER_GREP" "$snap" || true)"
  if [ -z "$lines" ]; then
    echo "# No marker matching $RS_TRACE_MARKER_GREP found in $snap" >> "$key"
    return 0
  fi

  echo "# Extracted windows around markers from: $snap" >> "$key"
  echo "# Match: $RS_TRACE_MARKER_GREP" >> "$key"
  echo "# Window: -$before .. +$after lines" >> "$key"
  echo >> "$key"

  printf "%s\n" "$lines" | while IFS= read -r line; do
    n="${line%%:*}"
    a=$((n - before))
    [ "$a" -lt 1 ] && a=1
    b=$((n + after))

    echo "==================== marker @ line $n ====================" >> "$key"
    sed -n "${a},${b}p" "$snap" \
      | grep -E "RS_|sched_(switch|wakeup|waking|migrate|stat_runtime)|dl_task_timer|start_dl_timer|inactive_task_timer|hrtimer_(start|expire_entry|expire_exit|cancel)" \
      >> "$key"
    echo >> "$key"
  done
}

need_root

[ "$#" -ge 2 ] && [ "$1" = "--" ] || die "usage: sudo $0 -- <command> [args...]"
shift

cmd0="$1"
stamp="$(now_stamp)"
base="$(cmd_basename "$cmd0")"
snap="snap_${base}_${stamp}.txt"
key="key_${base}_${stamp}.txt"

TR="$(tracing_dir)"

echo 0 > "$TR/tracing_on" 2>/dev/null || true
echo > "$TR/trace" 2>/dev/null || true

disable_all_events "$TR"
clear_filters "$TR"
set_clock "$TR"
set_buffer_kb "$TR" "$RS_TRACE_BUF_KB"

enable_event_if_exists "$TR" sched sched_switch
enable_event_if_exists "$TR" sched sched_wakeup
enable_event_if_exists "$TR" sched sched_waking
enable_event_if_exists "$TR" sched sched_wakeup_new
enable_event_if_exists "$TR" sched sched_migrate_task
enable_event_if_exists "$TR" sched sched_stat_runtime

if [ "$RS_TRACE_SCHEDSTAT_EXTRA" = "1" ]; then
  enable_event_if_exists "$TR" sched sched_stat_sleep
  enable_event_if_exists "$TR" sched sched_stat_wait
  enable_event_if_exists "$TR" sched sched_stat_blocked
  enable_event_if_exists "$TR" sched sched_stat_iowait
fi

enable_event_if_exists "$TR" hrtimer hrtimer_start
enable_event_if_exists "$TR" hrtimer hrtimer_cancel
enable_event_if_exists "$TR" hrtimer hrtimer_expire_entry
enable_event_if_exists "$TR" hrtimer hrtimer_expire_exit
enable_event_if_exists "$TR" ftrace tracing_mark_write

if [ "$RS_TRACE_IRQ" = "1" ]; then
  enable_event_if_exists "$TR" irq irq_handler_entry
  enable_event_if_exists "$TR" irq irq_handler_exit
  enable_event_if_exists "$TR" irq softirq_entry
  enable_event_if_exists "$TR" irq softirq_exit
fi

echo 1 > "$TR/tracing_on" 2>/dev/null || true

app_pid=""
pid_updater_pid=""

if [ "$RS_TRACE_EVENT_PID_AUTO" = "1" ]; then
  "$@" &
  app_pid=$!
  update_set_event_pid_loop "$TR" "$app_pid" &
  pid_updater_pid=$!
  wait "$app_pid" || true
  kill "$pid_updater_pid" 2>/dev/null || true
  wait "$pid_updater_pid" 2>/dev/null || true
else
  "$@" || true
fi

echo 0 > "$TR/tracing_on" 2>/dev/null || true
cat "$TR/trace" > "$snap"
extract_key_windows "$snap" "$key" "$RS_TRACE_WIN_BEFORE" "$RS_TRACE_WIN_AFTER"

echo "Saved:"
echo "  full: $snap"
echo "  key : $key"

if [ -e "$TR/set_event_pid" ]; then
  : > "$TR/set_event_pid" 2>/dev/null || true
fi
