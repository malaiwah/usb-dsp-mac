#!/usr/bin/env python3
"""
Test whether the DSP-408 needs a settling delay after set_configuration(),
or whether rapid-fire commands vs. spaced commands makes a difference.

The device ACKs our OUT packets but never responds on EP_IN.
Hypothesis: firmware USB stack needs time to reinitialize after USB reset
(SET_CONFIGURATION causes USB reset on STM32).

Run with: sudo .venv/bin/python3 timing_test.py
"""
from __future__ import annotations
import sys, time, os
os.environ['LIBUSB_DEBUG'] = '1'   # warnings only

import usb.core, usb.util

VID, PID = 0x0483, 0x5750
EP_OUT, EP_IN = 0x01, 0x82
REPORT_SZ = 64
IFACE = 0


def build_frame(cmd: int, data: bytes = b'') -> bytes:
    payload = bytes([cmd]) + data
    n = len(payload)
    chk = n
    for b in payload:
        chk ^= b
    raw = bytes([0x10, 0x02, 0x00, 0x01, n]) + payload + bytes([0x10, 0x03, chk & 0xFF])
    return raw.ljust(REPORT_SZ, b'\x00')


# Correct 1-byte OP_INIT frame (matches README: 10 02 00 01 01 10 10 03 11)
OP_INIT_FRAME  = build_frame(0x10)
OP_FW_FRAME    = build_frame(0x13)
OP_POLL_FRAME  = build_frame(0x40)
OP_INFO_FRAME  = build_frame(0x2C)

print(f"OP_INIT frame: {OP_INIT_FRAME[:12].hex(' ')}")
print(f"OP_FW   frame: {OP_FW_FRAME[:12].hex(' ')}")


def open_device(post_config_sleep: float = 0.0) -> usb.core.Device:
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise OSError("Device not found")
    print(f"Found: bus={dev.bus} addr={dev.address}")
    if dev.is_kernel_driver_active(IFACE):
        dev.detach_kernel_driver(IFACE)
        print("Detached kernel driver")
    try:
        dev.set_configuration()
        print("set_configuration() OK")
    except Exception as e:
        print(f"set_configuration() skipped: {e}")
    if post_config_sleep > 0:
        print(f"  sleeping {post_config_sleep}s for firmware settling...")
        time.sleep(post_config_sleep)
    usb.util.claim_interface(dev, IFACE)
    print("Interface claimed")
    return dev


def close_device(dev: usb.core.Device) -> None:
    try:
        usb.util.release_interface(dev, IFACE)
        dev.attach_kernel_driver(IFACE)
        print("Driver reattached")
    except Exception:
        pass
    usb.util.dispose_resources(dev)
    time.sleep(3)   # let DriverKit re-claim and re-init


def write_read(dev: usb.core.Device, label: str, frame: bytes,
               pre_read_ms: int = 0, post_write_sleep: float = 0,
               read_timeout_ms: int = 2000) -> bool:
    """Write frame, optionally pre-submit a read or sleep before read."""
    if pre_read_ms > 0:
        # Start a background read (we'll do it sequentially but quickly)
        pass

    n = dev.write(EP_OUT, frame, timeout=1000)
    print(f"  → {label}: wrote {n}B", end='')
    t0 = time.monotonic()

    if post_write_sleep > 0:
        time.sleep(post_write_sleep)

    try:
        data = bytes(dev.read(EP_IN, REPORT_SZ, timeout=read_timeout_ms))
        elapsed = time.monotonic() - t0
        print(f"  ← DATA after {elapsed*1000:.0f}ms: {data[:12].hex(' ')}")
        return True
    except usb.core.USBTimeoutError:
        elapsed = time.monotonic() - t0
        print(f"  ← TIMEOUT after {elapsed*1000:.0f}ms")
        return False


def test_scenario(label: str, post_config_sleep: float,
                  pre_write_sleep: float = 0, burst: bool = False):
    print(f"\n{'='*60}")
    print(f"SCENARIO: {label}")
    print(f"{'='*60}")
    dev = open_device(post_config_sleep=post_config_sleep)

    if pre_write_sleep > 0:
        print(f"Sleeping {pre_write_sleep}s before first write...")
        time.sleep(pre_write_sleep)

    if burst:
        # Write all commands without reading between them
        print("Burst writing 4 commands without reads...")
        for lbl, fr in [
            ("OP_INIT", OP_INIT_FRAME),
            ("OP_FW",   OP_FW_FRAME),
            ("OP_POLL", OP_POLL_FRAME),
            ("OP_INFO", OP_INFO_FRAME),
        ]:
            n = dev.write(EP_OUT, fr, timeout=1000)
            print(f"  → {lbl}: wrote {n}B")
            time.sleep(0.01)   # 10ms between writes
        # Now read for 3 seconds
        print("Reading for 3 seconds after burst...")
        deadline = time.monotonic() + 3.0
        count = 0
        while time.monotonic() < deadline:
            remaining = max(50, int((deadline - time.monotonic()) * 1000))
            try:
                data = bytes(dev.read(EP_IN, REPORT_SZ, timeout=min(remaining, 200)))
                print(f"  ← DATA[{count}]: {data[:12].hex(' ')}")
                count += 1
            except usb.core.USBTimeoutError:
                pass
        print(f"  Total received: {count} packets")
    else:
        # Sequential: write then read for each command
        for lbl, fr in [
            ("OP_INIT", OP_INIT_FRAME),
            ("OP_FW",   OP_FW_FRAME),
            ("OP_POLL", OP_POLL_FRAME),
            ("OP_INFO", OP_INFO_FRAME),
        ]:
            write_read(dev, lbl, fr, read_timeout_ms=1500)

    close_device(dev)


# ── Run scenarios ──────────────────────────────────────────────────────────────

# 1. No delay after set_configuration (baseline)
test_scenario("Baseline: no delay after set_config",
              post_config_sleep=0.0)

# 2. 500ms settling delay after set_configuration
test_scenario("500ms settling after set_config",
              post_config_sleep=0.5)

# 3. 2 second settling delay after set_configuration
test_scenario("2s settling after set_config",
              post_config_sleep=2.0)

# 4. 5 second settling delay
test_scenario("5s settling after set_config",
              post_config_sleep=5.0)

# 5. No set_configuration at all, 1s pre-write delay
test_scenario("No set_config, 1s pre-write delay",
              post_config_sleep=0.0, pre_write_sleep=1.0)

# 6. Burst: write all 4 commands quickly then read
test_scenario("Burst write then read 3s",
              post_config_sleep=0.0, burst=True)

print("\n=== All scenarios complete ===")
