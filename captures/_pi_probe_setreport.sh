#!/usr/bin/env bash
# Capture-2b: send the same WMCU-framed commands, but via HID SET_REPORT
# (control transfer, bmRequestType=0x21, bRequest=0x09) rather than via the
# interrupt OUT endpoint. Some HID devices only accept commands through
# SET_REPORT; commands on the interrupt pipe are silently dropped.
#
# Also polls for any response via HID GET_REPORT (bRequest=0x01) between
# sends, in case the device queues replies as feature/input reports.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "run as root"; exit 1; fi

OUT=/tmp/linux-02b-setreport-probe.pcapng

# Start capture
rm -f $OUT
dumpcap -q -i usbmon1 -w $OUT >/tmp/dumpcap.log 2>&1 &
DC=$!
sleep 1

python3 - <<'PY'
"""Try the vendor frame via three different paths:
   1. Interrupt OUT  (same as probe.py — known to get nothing back)
   2. SET_REPORT(OUTPUT, report_id=0) via control transfer
   3. SET_REPORT(FEATURE, report_id=0) via control transfer
   After each attempt, also issue GET_REPORT(INPUT) and GET_REPORT(FEATURE)
   to see if the device queued a reply.
"""
import hid, time, sys

VID, PID = 0x0483, 0x5750

def frame(cmd):
    n = 1
    chk = n ^ cmd
    body = bytes([0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk])
    return body + bytes(64 - len(body))

h = hid.device()
h.open(VID, PID)
print(f"opened: {h.get_product_string()} serial={h.get_serial_number_string()}")
h.set_nonblocking(1)   # so read() returns [] instantly if no data

CMD = 0x05  # firmware-version-ish guess

print(f"\n--- Path 1: interrupt OUT (write()) ---")
h.write(b"\x00" + frame(CMD))
for _ in range(10):
    r = h.read(64, 100)
    if r:
        print(f"  RX: {bytes(r).hex(' ', 1)}")
        break
    time.sleep(0.05)
else:
    print("  (no interrupt-IN response in 500 ms)")

print(f"\n--- Path 2: SET_REPORT(OUTPUT) via send_output_report ---")
try:
    # hid.device.send_output_report sends via interrupt OUT if available,
    # otherwise falls back to SET_REPORT — same as write(). So we need the
    # explicit control-transfer variant:
    if hasattr(h, "send_feature_report"):
        pass
    # Unfortunately, legacy cython-hidapi does NOT expose "write via SET_REPORT
    # over control". The best we can do from Python is a feature report:
    print("  (skipped — hidapi binds SET_REPORT to feature reports only)")
except Exception as e:
    print(f"  error: {e}")

print(f"\n--- Path 3: SET_REPORT(FEATURE) via send_feature_report ---")
try:
    # Report ID byte prepended as for write()
    sent = h.send_feature_report(b"\x00" + frame(CMD))
    print(f"  send_feature_report returned: {sent}")
except Exception as e:
    print(f"  send_feature_report raised: {e}")

print(f"\n--- Path 4: GET_REPORT(INPUT, id=0) ---")
try:
    r = h.get_input_report(0, 64)
    print(f"  GET_REPORT(INPUT) returned {len(r)} bytes: {bytes(r).hex(' ', 1)[:96]}")
except Exception as e:
    print(f"  get_input_report raised: {e}")

print(f"\n--- Path 5: GET_REPORT(FEATURE, id=0) ---")
try:
    r = h.get_feature_report(0, 64)
    print(f"  GET_REPORT(FEATURE) returned {len(r)} bytes: {bytes(r).hex(' ', 1)[:96]}")
except Exception as e:
    print(f"  get_feature_report raised: {e}")

# Drain one last time
print(f"\n--- Tail drain (interrupt IN) ---")
for _ in range(5):
    r = h.read(64, 200)
    if r:
        print(f"  RX: {bytes(r).hex(' ', 1)[:96]}")
    else:
        break
h.close()
PY

sleep 1
kill -INT $DC; wait $DC 2>/dev/null || true
ls -lh $OUT
