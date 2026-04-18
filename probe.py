#!/usr/bin/env python3
"""
Probe the DSP-408 over USB HID.

Confirmed from firmware analysis (DSP-408-Firmware.bin):
  VID=0x0483 (STMicroelectronics), PID=0x5750
  USB product string: "Audio_Equipment"
  HID Report Descriptor: 64-byte IN + 64-byte OUT, NO report ID, Usage Page 0x8C (vendor)
  MCU: STM32 Cortex-M, app starts at 0x08005000, ~18KB RAM

hidapi on macOS: dev.write() expects report_id byte first even if device has no report ID.
We prefix 0x00 so the OS strips it; the STM32 receives exactly 64 bytes.
"""
import sys
import hid
import time

CONFIRMED_VID = 0x0483
CONFIRMED_PID = 0x5750
REPORT_SIZE = 64  # device reports are exactly 64 bytes (no report ID in descriptor)


def send_recv(dev, payload: bytes, label: str = "", timeout_ms: int = 500) -> bytes | None:
    # hidapi on macOS requires report_id as first byte; 0x00 = no report ID
    buf = bytes([0x00]) + payload[:REPORT_SIZE] + bytes(max(0, REPORT_SIZE - len(payload)))
    print(f"  OUT [{label}]: {buf[1:].hex()[:48]}...")
    dev.write(buf)

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        resp = dev.read(65, timeout_ms=100)
        if resp:
            print(f"  IN  [{label}]: {bytes(resp).hex()[:48]}...")
            return bytes(resp)
        time.sleep(0.01)
    print(f"  IN  [{label}]: (no response)")
    return None


def probe(dev):
    probes = [
        # Likely IAP_DSP_REQUEST_INFO equivalent — try a few command bytes
        (b'\x01' + b'\x00' * 63, "cmd_01"),
        (b'\x02' + b'\x00' * 63, "cmd_02"),
        (b'\x11' + b'\x00' * 63, "cmd_11_handshake?"),
        (b'\x13' + b'\x00' * 63, "cmd_13_devinfo?"),
        # Try the t.racks-style DLE/STX framing over HID
        (bytes([0x10, 0x02, 0x00, 0x01, 0x01, 0x01, 0x10, 0x03, 0x11]), "t_racks_handshake"),
        (bytes([0x10, 0x02, 0x00, 0x01, 0x01, 0x13, 0x10, 0x03, 0x12]), "t_racks_devinfo"),
        # Raw 0x01 report-style (some HID DSPs use first byte as command)
        (bytes([0xB0]) + b'\x00' * 63, "cmd_B0"),
        (bytes([0xA0]) + b'\x00' * 63, "cmd_A0"),
    ]

    for payload, label in probes:
        send_recv(dev, payload, label, timeout_ms=300)
        time.sleep(0.05)


def main():
    if len(sys.argv) < 3:
        print("Usage: probe.py <VID_hex> <PID_hex>")
        print("  Run discover.py first to find VID/PID")
        sys.exit(1)

    vid = int(sys.argv[1], 16)
    pid = int(sys.argv[2], 16)

    print(f"Probing HID device VID=0x{vid:04x} PID=0x{pid:04x}")
    dev = hid.device()
    dev.open(vid, pid)
    dev.set_nonblocking(False)

    print(f"  {dev.get_manufacturer_string()} / {dev.get_product_string()}")
    print()

    probe(dev)

    dev.close()
    print("\nDone.")


if __name__ == '__main__':
    main()
