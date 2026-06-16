#!/bin/sh
set -eux

CPUFREQ_BASE="/sys/devices/system/cpu/cpufreq"
TARGET_ARG="${1:---max}"

# must be root
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root: sudo $0 [target_khz|--max|--min]" >&2
  exit 1
fi

if [ ! -d "$CPUFREQ_BASE" ]; then
  echo "ERROR: $CPUFREQ_BASE not found; cpufreq not available?" >&2
  exit 1
fi

case "$TARGET_ARG" in
  --max)
    mode="max"
    ;;
  --min)
    mode="min"
    ;;
  ''|*[!0-9]*)
    echo "ERROR: expected [target_khz|--max|--min], got '$TARGET_ARG'" >&2
    exit 1
    ;;
  *)
    mode="fixed"
    TARGET_KHZ="$TARGET_ARG"
    ;;
esac

if [ "$mode" = "fixed" ]; then
  echo "Target frequency: ${TARGET_KHZ} kHz"
else
  echo "Target frequency mode: --${mode}"
fi

# Disable boost (generic)
if [ -f "$CPUFREQ_BASE/boost" ]; then
  echo 0 > "$CPUFREQ_BASE/boost"
  echo "Set $CPUFREQ_BASE/boost = 0 (boost disabled)"
else
  echo "Note: $CPUFREQ_BASE/boost not present"
fi

# Disable turbo (intel pstate, if present)
if [ -f "/sys/devices/system/cpu/intel_pstate/no_turbo" ]; then
  echo 1 > "/sys/devices/system/cpu/intel_pstate/no_turbo"
  echo "Set /sys/devices/system/cpu/intel_pstate/no_turbo = 1 (turbo disabled)"
else
  echo "Note: /sys/devices/system/cpu/intel_pstate/no_turbo not present"
fi

echo ""

# Iterate policies (POSIX-safe glob)
found=0
for p in "$CPUFREQ_BASE"/policy*; do
  if [ ! -d "$p" ]; then
    continue
  fi
  found=1

  minf="$(cat "$p/cpuinfo_min_freq" 2>/dev/null || echo 0)"
  maxf="$(cat "$p/cpuinfo_max_freq" 2>/dev/null || echo 0)"
  drv="$(cat "$p/scaling_driver" 2>/dev/null || echo unknown)"

  case "$mode" in
    max)
      target_khz="$maxf"
      ;;
    min)
      target_khz="$minf"
      ;;
    fixed)
      target_khz="$TARGET_KHZ"
      ;;
  esac

  # range check
  if [ "$target_khz" -lt "$minf" ] || [ "$target_khz" -gt "$maxf" ]; then
    echo "WARN: ${p##*/}: target ${target_khz} outside hw range [${minf}, ${maxf}] kHz; skipping"
    continue
  fi

  # Best-effort set governor to performance if available
  if [ -f "$p/scaling_available_governors" ] && grep -q "performance" "$p/scaling_available_governors"; then
    echo performance > "$p/scaling_governor" 2>/dev/null || true
  fi

  # Lock min/max
  echo "$target_khz" > "$p/scaling_min_freq"
  echo "$target_khz" > "$p/scaling_max_freq"

  # Optional: set speed if interface exists
  if [ -f "$p/scaling_setspeed" ]; then
    echo "$target_khz" > "$p/scaling_setspeed" 2>/dev/null || true
  fi

  gov="$(cat "$p/scaling_governor" 2>/dev/null || echo n/a)"
  cur="$(cat "$p/scaling_cur_freq" 2>/dev/null || echo n/a)"
  echo "OK: ${p##*/}: driver=$drv target=$target_khz gov=$gov min=$(cat "$p/scaling_min_freq") max=$(cat "$p/scaling_max_freq") cur=$cur"
done

if [ "$found" -eq 0 ]; then
  echo "ERROR: No policies found under $CPUFREQ_BASE/policy*" >&2
  exit 1
fi

echo ""
echo "Summary (scaling_cur_freq):"
grep -H . "$CPUFREQ_BASE"/policy*/scaling_cur_freq 2>/dev/null || true
