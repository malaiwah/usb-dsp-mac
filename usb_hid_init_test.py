#!/usr/bin/env python3
"""
Test: HID class initialization before interrupt IN reads.

Hypothesis: The DSP-408 firmware requires the HID class initialization
sequence (SET_IDLE, SET_PROTOCOL) before it generates interrupt IN
responses. DriverKit sends these automatically during open(); we've been
skipping them.

Also tests: skipping set_configuration() which can cause an implicit
USB reset and may clear device state.

Run with: sudo .venv/bin/python3 usb_hid_init_test.py
"""

from __future__ import annotations
import sys
import time
import usb.core
import usb.util

VID = 0x0483
PID = 0x5750
EP_OUT    = 0x01
EP_IN     = 0x82
REPORT_SZ = 64
INTERFACE = 0

# HID class bmRequestType
HID_HOST_TO_DEV = 0x21   # Class | Interface | Host→Device
HID_DEV_TO_HOST = 0xA1   # Class | Interface | Device→Host

# HID class requests
HID_GET_REPORT   = 0x01
HID_GET_IDLE     = 0x02
HID_GET_PROTOCOL = 0x03
HID_SET_REPORT   = 0x09
HID_SET_IDLE     = 0x0A
HID_SET_PROTOCOL = 0x0B

# HID report types (for GET/SET_REPORT wValue high byte)
HID_INPUT_REPORT  = 0x01
HID_OUTPUT_REPORT = 0x02
HID_FEATURE_REPORT = 0x03


def build_frame(cmd: int, data: list = None) -> bytes:
    payload = [cmd] + (list(data) if data else [])
    length = len(payload)
    chk = length
    for b in payload:
        chk ^= b
    frame = bytes([0x10, 0x02, 0x00, 0x01, length] + payload + [0x10, 0x03, chk])
    return frame.ljust(REPORT_SZ, b'\x00')


def parse_frame(raw: bytes):
    if len(raw) < 7:
        return None
    try:
        idx = raw.index(0x10)
        if raw[idx + 1] != 0x02:
            return None
        length = raw[idx + 4]
        etx = idx + 5 + length
        if raw[etx:etx + 2] != bytes([0x10, 0x03]):
            return None
        payload = raw[idx + 5:etx]
        chk = raw[etx + 2]
        calc = length
        for b in payload:
            calc ^= b
        if chk != calc:
            return None
        return payload
    except (ValueError, IndexError):
        return None


def ctrl(dev, direction, req, val, idx, length=0, data=None, label=""):
    """Wrapper for ctrl_transfer with human-readable error reporting."""
    try:
        if data is not None:
            ret = dev.ctrl_transfer(direction, req, val, idx, data, timeout=500)
        elif length > 0:
            ret = dev.ctrl_transfer(direction, req, val, idx, length, timeout=500)
        else:
            ret = dev.ctrl_transfer(direction, req, val, idx, None, timeout=500)
        print(f"  {label}: OK → {bytes(ret).hex() if hasattr(ret, '__iter__') and length else ret}")
        return ret
    except usb.core.USBError as e:
        print(f"  {label}: FAILED — {e}")
        return None
    except Exception as e:
        print(f"  {label}: ERROR — {e}")
        return None


def try_read(dev, label, timeout_ms=1000):
    try:
        data = bytes(dev.read(EP_IN, REPORT_SZ, timeout=timeout_ms))
        payload = parse_frame(data)
        if payload:
            print(f"  ← {label}: PARSED cmd={payload[0]:#04x} payload={payload[1:].hex()} ({len(payload)-1}B)")
        else:
            print(f"  ← {label}: RAW {data[:16].hex(' ')}...")
        return data
    except usb.core.USBTimeoutError:
        print(f"  ← {label}: TIMEOUT")
        return None


def run_test(skip_set_config: bool, hid_init: bool):
    tag = f"[skip_cfg={skip_set_config} hid_init={hid_init}]"
    print(f"\n{'='*60}")
    print(f"TEST {tag}")
    print(f"{'='*60}")

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("Device not found!")
        return

    print(f"Found: bus={dev.bus} addr={dev.address}")

    # Detach driver
    try:
        active = dev.is_kernel_driver_active(INTERFACE)
    except Exception:
        active = False

    if active:
        print("Detaching kernel driver...")
        try:
            dev.detach_kernel_driver(INTERFACE)
            print("  → Detached")
        except usb.core.USBError as e:
            print(f"  → Detach failed: {e}")
            return

    # SET_CONFIGURATION (optionally skip)
    if not skip_set_config:
        print("SET_CONFIGURATION...")
        try:
            dev.set_configuration()
            print("  → OK")
        except usb.core.USBError as e:
            print(f"  → {e} (ignored)")

    # Claim interface
    try:
        usb.util.claim_interface(dev, INTERFACE)
        print("Interface claimed")
    except usb.core.USBError as e:
        print(f"Claim failed: {e}")
        usb.util.dispose_resources(dev)
        return

    # ── HID Class Initialization ──────────────────────────────────────────
    if hid_init:
        print("\n-- HID class init --")

        # GET_PROTOCOL (what mode is device in?)
        ctrl(dev, HID_DEV_TO_HOST, HID_GET_PROTOCOL, 0, INTERFACE, length=1,
             label="GET_PROTOCOL")

        # GET_IDLE (what idle rate is set?)
        ctrl(dev, HID_DEV_TO_HOST, HID_GET_IDLE, 0, INTERFACE, length=1,
             label="GET_IDLE(report 0)")

        # SET_PROTOCOL = 1 (report protocol; 0 = boot protocol)
        ctrl(dev, HID_HOST_TO_DEV, HID_SET_PROTOCOL, 1, INTERFACE,
             label="SET_PROTOCOL(report)")

        # SET_IDLE = 0 (report only when data changes; 0 = no idle suppression polling)
        # wValue = (duration << 8) | reportID; duration=0, reportID=0
        ctrl(dev, HID_HOST_TO_DEV, HID_SET_IDLE, 0x0000, INTERFACE,
             label="SET_IDLE(0,0)")

    # ── Pre-read (catch any unsolicited reports) ──────────────────────────
    print("\n-- Pre-read (200ms, expect timeout) --")
    try_read(dev, "pre-read", timeout_ms=200)

    # ── Protocol commands ─────────────────────────────────────────────────
    tests = [
        ("OP_INIT  0x10", build_frame(0x10, [0x10])),
        ("OP_FW   0x13",  build_frame(0x13, [0x13])),
        ("OP_POLL 0x40",  build_frame(0x40, [0x40])),
    ]

    for label, frame in tests:
        print(f"\n→ {label}")
        n = dev.write(EP_OUT, frame, timeout=1000)
        print(f"  write: {n}B")
        try_read(dev, "after write", timeout_ms=1500)

    # ── Also try GET_REPORT via control endpoint ──────────────────────────
    print("\n-- GET_REPORT via control endpoint --")
    ctrl(dev, HID_DEV_TO_HOST, HID_GET_REPORT, (HID_INPUT_REPORT << 8) | 0,
         INTERFACE, length=REPORT_SZ, label="GET_REPORT(input, 0)")

    # Cleanup
    print("\n-- Cleanup --")
    try:
        usb.util.release_interface(dev, INTERFACE)
        try:
            dev.attach_kernel_driver(INTERFACE)
            print("Driver reattached")
        except Exception:
            pass
    except Exception:
        pass
    usb.util.dispose_resources(dev)
    print("Done")
    time.sleep(2)  # Let device settle / DriverKit re-claim


def main():
    print("=== DSP-408 HID Init Test ===")
    print("(requires root — run with sudo)\n")

    if not any('sudo' in arg for arg in sys.argv) and sys.stdin.isatty():
        import os
        if os.getuid() != 0:
            print("WARNING: not running as root. Detach may fail.\n")

    # Test 1: Normal flow (with set_config) but WITH hid_init
    run_test(skip_set_config=False, hid_init=True)

    # Test 2: Skip set_configuration, WITH hid_init
    # (avoid the implicit USB reset from SET_CONFIGURATION)
    run_test(skip_set_config=True, hid_init=True)

    # Test 3: Normal flow, WITHOUT hid_init (baseline comparison)
    run_test(skip_set_config=False, hid_init=False)


if __name__ == '__main__':
    main()
