#!/usr/bin/env python3
"""flash_firmware.py — reflash DSP-408 firmware over USB HID by VID/PID.

Bypasses DSP-408.exe (which uses HID Usage Page to find device).
Works even after a HID descriptor patch that breaks app detection.

Usage:
    python flash_firmware.py downloads/DSP-408-Firmware-V6.21.bin

Requirements:
    pip install hidapi
"""
import sys
import struct
import time
from pathlib import Path

VID = 0x0483
PID = 0x5750
FW_HEADER_SIZE = 8      # "WMCU\x08\x00P\x00" prefix in .bin, skipped in upload
BLOCK_DATA_SIZE = 48    # bytes of firmware per HID packet
CMD_CONNECT = 0xcc
CMD_FW_UPLOAD = 0x38
DIR_CMD = 0xa2          # host→device normal
DIR_FW  = 0xa1          # host→device firmware block
DIR_RESP = 0x53         # device→host normal
DIR_FW_ACK = 0x51       # device→host firmware ack


def build_frame(direction: int, seq: int, cmd: int, data: bytes) -> bytes:
    """Build a 64-byte DSP-408 HID frame."""
    data_len = len(data)
    header = bytes([0x80, 0x80, 0x80, 0xee, direction, 0x01, seq & 0xff, 0x09])
    header += struct.pack("<I", cmd)
    header += struct.pack("<H", data_len)
    body = header + data
    chk = 0
    for b in body[4:]:
        chk ^= b
    frame = body + bytes([chk, 0xaa])
    frame += b'\x00' * (64 - len(frame))
    return frame[:64]


def send_recv(dev, direction: int, seq: int, cmd: int, data: bytes = b'\x00' * 8) -> bytes:
    frame = build_frame(direction, seq, cmd, data)
    dev.write(bytes([0x00]) + frame)  # report ID 0 prefix
    resp = dev.read(64, timeout_ms=2000)
    return bytes(resp) if resp else b''


def connect(dev) -> bool:
    resp = send_recv(dev, DIR_CMD, 0, CMD_CONNECT)
    if not resp or resp[4] != DIR_RESP or resp[8] != CMD_CONNECT:
        print("Connect failed")
        return False
    status = resp[14] if len(resp) > 14 else 0xff
    if status != 0:
        print(f"Connect returned status {status:#04x}")
        return False
    print("Connected.")
    return True


def flash(dev, fw_path: Path) -> bool:
    fw_data = fw_path.read_bytes()
    if fw_data[:4] == b'WMCU':
        fw_data = fw_data[FW_HEADER_SIZE:]
    total = len(fw_data)
    blocks = (total + BLOCK_DATA_SIZE - 1) // BLOCK_DATA_SIZE
    print(f"Firmware: {total} bytes, {blocks} blocks of {BLOCK_DATA_SIZE} bytes")

    for i in range(blocks):
        block = fw_data[i * BLOCK_DATA_SIZE:(i + 1) * BLOCK_DATA_SIZE]
        block = block.ljust(BLOCK_DATA_SIZE, b'\x00')
        resp = send_recv(dev, DIR_FW, i & 0xff, CMD_FW_UPLOAD, block)
        if not resp or resp[4] != DIR_FW_ACK:
            print(f"\nNo ack for block {i}")
            return False
        if (i + 1) % 100 == 0 or i == blocks - 1:
            pct = (i + 1) * 100 // blocks
            print(f"\r  {i+1}/{blocks} blocks ({pct}%)", end="", flush=True)
    print()
    print("Flash complete.")
    return True


def main():
    try:
        import hid
    except ImportError:
        print("Install hidapi first:  pip install hidapi")
        sys.exit(1)

    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <firmware.bin>")
        sys.exit(1)

    fw_path = Path(sys.argv[1])
    if not fw_path.exists():
        print(f"File not found: {fw_path}")
        sys.exit(1)

    print(f"Looking for VID={VID:#06x} PID={PID:#06x} ...")
    devs = hid.enumerate(VID, PID)
    if not devs:
        print("Device not found. Is it plugged in?")
        sys.exit(1)

    # Pick interface 0 (interrupt OUT is on the first HID interface)
    path = devs[0]['path']
    print(f"Found: {devs[0].get('product_string','?')} on {path}")

    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(0)

    try:
        if not connect(dev):
            sys.exit(1)
        if not flash(dev, fw_path):
            sys.exit(1)
        print("Done. Unplug and replug the device.")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
