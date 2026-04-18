#!/usr/bin/env python3
"""
Test: Send DSP-408 commands via HID SET_REPORT (control endpoint)
instead of the interrupt OUT endpoint.

Hypothesis: DriverKit's IOHIDDeviceSetReport sends output reports via
the HID control-path SET_REPORT request (bmRequestType=0x21, bRequest=0x09).
The device firmware may only process commands arriving this way, NOT
via the interrupt OUT endpoint (EP_OUT=0x01).

Run with: sudo .venv/bin/python set_report_test.py
"""
from __future__ import annotations
import sys, time
import usb.core, usb.util

VID, PID = 0x0483, 0x5750
EP_OUT, EP_IN = 0x01, 0x82
REPORT_SZ = 64
IFACE = 0

# HID control-path constants
HID_REQTYPE_OUT = 0x21   # Class | Interface | Host→Device
HID_SET_REPORT  = 0x09
HID_OUTPUT_RPT  = 0x02   # report type: output
HID_INPUT_RPT   = 0x01   # report type: input
REPORT_ID       = 0x00   # no report ID


def build_frame(cmd: int, data: bytes = b'') -> bytes:
    """Build correct 64-byte DLE/STX frame."""
    payload = bytes([cmd]) + data
    n = len(payload)
    chk = n
    for b in payload:
        chk ^= b
    raw = bytes([0x10, 0x02, 0x00, 0x01, n]) + payload + bytes([0x10, 0x03, chk & 0xFF])
    return raw.ljust(REPORT_SZ, b'\x00')


CMDS = [
    ("OP_INIT  0x10", build_frame(0x10)),
    ("OP_FW   0x13",  build_frame(0x13)),
    ("OP_INFO 0x2C",  build_frame(0x2C)),
    ("OP_POLL 0x40",  build_frame(0x40)),
]


def open_dev() -> usb.core.Device:
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise OSError("Device not found")
    print(f"Found: bus={dev.bus} addr={dev.address}")
    if dev.is_kernel_driver_active(IFACE):
        dev.detach_kernel_driver(IFACE)
    try:
        dev.set_configuration()
    except Exception:
        pass
    usb.util.claim_interface(dev, IFACE)
    return dev


def close_dev(dev: usb.core.Device) -> None:
    try:
        usb.util.release_interface(dev, IFACE)
        dev.attach_kernel_driver(IFACE)
    except Exception:
        pass
    usb.util.dispose_resources(dev)
    time.sleep(3)


def try_read(dev: usb.core.Device, timeout_ms: int = 1500) -> bytes | None:
    try:
        return bytes(dev.read(EP_IN, REPORT_SZ, timeout=timeout_ms))
    except usb.core.USBTimeoutError:
        return None


# ── Test 1: SET_REPORT via control endpoint ────────────────────────────────────
print("=" * 60)
print("TEST 1: Commands via HID SET_REPORT (control endpoint 0x00)")
print("=" * 60)
dev = open_dev()

for label, frame in CMDS:
    print(f"\n→ {label}")

    # Send via SET_REPORT (control)
    try:
        ret = dev.ctrl_transfer(
            HID_REQTYPE_OUT,
            HID_SET_REPORT,
            (HID_OUTPUT_RPT << 8) | REPORT_ID,  # wValue: report type + ID
            IFACE,                                # wIndex: interface
            frame,                                # data = 64-byte report
            timeout=1000
        )
        print(f"  SET_REPORT: OK ({ret}B sent)")
    except usb.core.USBError as e:
        print(f"  SET_REPORT: FAILED — {e}")
        continue

    # Now read from EP_IN
    data = try_read(dev, 1500)
    if data:
        print(f"  ← EP_IN DATA: {data[:16].hex(' ')}")
    else:
        print(f"  ← EP_IN TIMEOUT")

close_dev(dev)

# ── Test 2: Interleave SET_REPORT and EP_OUT ──────────────────────────────────
print("\n" + "=" * 60)
print("TEST 2: SET_REPORT first, then EP_OUT, combined read")
print("=" * 60)
dev = open_dev()

label, frame = CMDS[0]   # OP_INIT
print(f"\n→ {label}")

# Step 1: SET_REPORT
try:
    dev.ctrl_transfer(HID_REQTYPE_OUT, HID_SET_REPORT,
                      (HID_OUTPUT_RPT << 8) | REPORT_ID,
                      IFACE, frame, timeout=1000)
    print("  SET_REPORT: sent")
except usb.core.USBError as e:
    print(f"  SET_REPORT: {e}")

# Step 2: Also write to EP_OUT
n = dev.write(EP_OUT, frame, timeout=1000)
print(f"  EP_OUT write: {n}B")

# Step 3: Read (2s timeout)
data = try_read(dev, 2000)
print(f"  ← {'DATA: ' + data[:16].hex(' ') if data else 'TIMEOUT'}")

close_dev(dev)

# ── Test 3: Pre-read (submit EP_IN read FIRST), then SET_REPORT ───────────────
# Can't easily pre-read synchronously; do this with a thread
print("\n" + "=" * 60)
print("TEST 3: Thread-based pre-read, then SET_REPORT")
print("=" * 60)
import threading

dev = open_dev()
recv_buf = [None]

def reader_thread():
    try:
        recv_buf[0] = bytes(dev.read(EP_IN, REPORT_SZ, timeout=4000))
    except usb.core.USBTimeoutError:
        recv_buf[0] = b''
    except Exception as e:
        recv_buf[0] = f'ERROR: {e}'.encode()

t = threading.Thread(target=reader_thread, daemon=True)
t.start()
time.sleep(0.05)   # Let read get submitted

label, frame = CMDS[0]   # OP_INIT
print(f"\n→ {label} via SET_REPORT (while read is pending)...")
try:
    dev.ctrl_transfer(HID_REQTYPE_OUT, HID_SET_REPORT,
                      (HID_OUTPUT_RPT << 8) | REPORT_ID,
                      IFACE, frame, timeout=1000)
    print("  SET_REPORT: sent")
except usb.core.USBError as e:
    print(f"  SET_REPORT: {e}")

print("  Waiting for reader thread (up to 4s)...")
t.join(timeout=5)
result = recv_buf[0]
if result:
    if isinstance(result, bytes) and len(result) == 0:
        print("  ← TIMEOUT")
    elif isinstance(result, bytes):
        print(f"  ← DATA: {result[:16].hex(' ')}")
    else:
        print(f"  ← {result}")
else:
    print("  ← (thread still running?)")

close_dev(dev)

print("\n=== Done ===")
