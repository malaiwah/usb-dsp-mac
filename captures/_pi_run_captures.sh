#!/usr/bin/env bash
# Run on the Pi: capture both enum+idle and vendor-probe pcaps.
# Saves to /tmp/linux-01-enum-idle.pcapng and /tmp/linux-02-vendor-probe.pcapng.
#
# Usage:  sudo ./_pi_run_captures.sh
# (sudo is required for dumpcap on usbmon and for /dev/hidraw0.)
set -euo pipefail

DEV_SYSFS="/sys/bus/usb/devices/1-1.5"      # DSP-408 port path on this Pi
USBMON_IF="usbmon1"                         # bus 1
PROBE_PY="$(dirname "$(readlink -f "$0")")/probe.py"
OUT1="/tmp/linux-01-enum-idle.pcapng"
OUT2="/tmp/linux-02-vendor-probe.pcapng"

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (need usbmon + /dev/hidraw0)"; exit 1
fi
if [[ ! -e "$DEV_SYSFS/idVendor" ]]; then
  echo "DSP-408 not present at $DEV_SYSFS"; exit 1
fi

cleanup() {
  if [[ -n "${DUMPCAP_PID:-}" ]] && kill -0 "$DUMPCAP_PID" 2>/dev/null; then
    kill -INT "$DUMPCAP_PID" 2>/dev/null || true
    wait "$DUMPCAP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_capture() {
  local out="$1"
  echo "  → starting dumpcap on $USBMON_IF -> $out"
  rm -f "$out"
  # pcapng is dumpcap's default; this build dropped the -F flag
  dumpcap -q -i "$USBMON_IF" -w "$out" >/tmp/dumpcap.log 2>&1 &
  DUMPCAP_PID=$!
  # Give dumpcap a moment to actually attach to usbmon
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.3
    [[ -s "$out" ]] && return 0
  done
  echo "WARN: dumpcap output still empty after 3s — continuing anyway"
}

stop_capture() {
  if [[ -n "${DUMPCAP_PID:-}" ]] && kill -0 "$DUMPCAP_PID" 2>/dev/null; then
    kill -INT "$DUMPCAP_PID"
    wait "$DUMPCAP_PID" 2>/dev/null || true
  fi
  DUMPCAP_PID=""
}

wait_for_device() {
  local n=0
  while [[ ! -e "$DEV_SYSFS/idVendor" || ! -e /dev/hidraw0 ]]; do
    sleep 0.2
    n=$((n+1))
    if (( n > 100 )); then
      echo "device did not reappear after 20s"; return 1
    fi
  done
}

############################################################################
# CAPTURE 1: enum + idle
############################################################################
echo "=== CAPTURE 1: enumeration + 30s idle ==="
start_capture "$OUT1"

echo "  → triggering re-enum via authorized=0/1"
echo 0 > "$DEV_SYSFS/authorized"
sleep 4
echo 1 > "$DEV_SYSFS/authorized"
wait_for_device

echo "  → idling 30s (watching for any unsolicited input reports)"
sleep 30

stop_capture
ls -lh "$OUT1"
echo

############################################################################
# CAPTURE 2: vendor probe
############################################################################
echo "=== CAPTURE 2: vendor probe ==="
if [[ ! -f "$PROBE_PY" ]]; then
  echo "missing probe.py at $PROBE_PY"; exit 1
fi
start_capture "$OUT2"

echo "  → running probe.py …"
python3 "$PROBE_PY" || echo "(probe.py exit non-zero; capture still saved)"

# Give a small tail window to catch any late input reports
sleep 1
stop_capture
ls -lh "$OUT2"
echo
echo "All done. Pull files with:"
echo "  scp pi@10.21.0.19:$OUT1 ."
echo "  scp pi@10.21.0.19:$OUT2 ."
