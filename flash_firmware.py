#!/usr/bin/env python3
"""flash_firmware.py — reflash DSP-408 firmware over USB HID by VID/PID.

Bypasses DSP-408.exe (which uses HID Usage Page to find device).
Works even after a HID descriptor patch that breaks app detection.

Usage:
    python flash_firmware.py downloads/DSP-408-Firmware-V6.21.bin

Requirements:
    pip install hidapi

Complete firmware upload sequence (reverse-engineered from Windows captures):
  1. Send 65x 64-byte frames of 0x43 0x11 repeating — enters firmware mode
  2. cmd=0x36 (dir=a1): 16 zero bytes — prepare
  3. cmd=0x37 (dir=a1): 4 bytes from WMCU header offset 4 — firmware metadata
  4. cmd=0x38 (dir=a1) x 1465: 48-byte firmware blocks (data from .bin offset 8)
  5. cmd=0x39 (dir=a1): 1 byte 0x13 — apply firmware and reboot
"""
import sys
import struct
import time
from pathlib import Path

VID = 0x0483
PID = 0x5750
FW_HEADER_SIZE = 8      # "WMCU\x08\x00P\x00" prefix in .bin, skipped in upload
BLOCK_DATA_SIZE = 48    # bytes of firmware per HID packet
FW_MODE_FRAMES = 65     # number of 0x43 0x11 trigger frames

CMD_CONNECT   = 0xcc
CMD_FW_PREP   = 0x36    # prepare for upload (16 zero bytes)
CMD_FW_META   = 0x37    # firmware metadata (bytes 4-7 of WMCU header)
CMD_FW_UPLOAD = 0x38    # firmware block (48 bytes of fw data)
CMD_FW_APPLY  = 0x39    # finalize: apply firmware and reboot

DIR_CMD    = 0xa2       # host→device normal
DIR_FW     = 0xa1       # host→device firmware mode
DIR_RESP   = 0x53       # device→host normal
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
    if not resp or resp[4] not in (DIR_RESP, DIR_FW_ACK):
        print("Connect: no response (proceeding anyway — device may be in firmware mode)")
        return True   # proceed regardless; firmware mode entry will handle it
    status = resp[14] if len(resp) > 14 else 0xff
    if status != 0:
        print(f"Connect returned status {status:#04x} (non-zero — device may already be in firmware mode, proceeding)")
    else:
        print("Connected.")
    return True


def enter_firmware_mode(dev) -> None:
    """Send 65 frames of 0x43 0x11 repeating to trigger firmware upload mode."""
    trigger = bytes([0x43, 0x11] * 32)  # 64 bytes
    for _ in range(FW_MODE_FRAMES):
        dev.write(bytes([0x00]) + trigger)
        dev.read(64, timeout_ms=100)    # drain any response, don't care about content
    print("Firmware mode entered.")


def reopen(path: bytes) -> object:
    """Close and reopen after USB re-enumeration triggered by cmd=0x36."""
    import hid
    print("  Waiting for USB re-enumeration...", end="", flush=True)
    for _ in range(30):
        time.sleep(0.5)
        try:
            d = hid.device()
            d.open_path(path)
            d.set_nonblocking(0)
            print(" done.")
            return d
        except OSError:
            print(".", end="", flush=True)
    print()
    return None


def flash(path: bytes, fw_path: Path) -> bool:
    import hid
    raw = fw_path.read_bytes()
    if raw[:4] != b'WMCU':
        print("Error: .bin file missing WMCU header")
        return False
    wmcu_meta = raw[4:8]        # bytes 4-7 of header (sent in cmd=0x37)
    fw_data = raw[FW_HEADER_SIZE:]
    total = len(fw_data)
    blocks = (total + BLOCK_DATA_SIZE - 1) // BLOCK_DATA_SIZE
    print(f"Firmware: {total} bytes, {blocks} blocks of {BLOCK_DATA_SIZE} bytes")

    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(0)

    # Step 1: connect
    if not connect(dev):
        dev.close()
        return False

    # Step 2: enter firmware mode (65x 0x43 0x11 frames)
    enter_firmware_mode(dev)

    # Step 3: cmd=0x36 — prepare; this triggers USB re-enumeration on the host side
    resp = send_recv(dev, DIR_FW, 0, CMD_FW_PREP, bytes(16))
    dev.close()         # handle will be invalid after re-enumeration
    if not resp or resp[4] != DIR_FW_ACK:
        print("Prepare (0x36) failed")
        return False

    # Step 4: reopen after re-enumeration
    dev = reopen(path)
    if dev is None:
        print("Device did not re-enumerate")
        return False

    # Step 5: cmd=0x37 — firmware metadata (bytes 4-7 of WMCU header)
    resp = send_recv(dev, DIR_FW, 0, CMD_FW_META, wmcu_meta)
    if not resp or resp[4] != DIR_FW_ACK:
        print(f"Metadata (0x37) failed (resp={resp[:8].hex() if resp else 'none'})")
        dev.close()
        return False

    # Step 6: cmd=0x38 — firmware blocks
    print("Uploading...")
    for i in range(blocks):
        block = fw_data[i * BLOCK_DATA_SIZE:(i + 1) * BLOCK_DATA_SIZE]
        block = block.ljust(BLOCK_DATA_SIZE, b'\x00')
        resp = send_recv(dev, DIR_FW, i & 0xff, CMD_FW_UPLOAD, block)
        if not resp or resp[4] != DIR_FW_ACK:
            print(f"\nNo ack for block {i}")
            dev.close()
            return False
        if (i + 1) % 100 == 0 or i == blocks - 1:
            pct = (i + 1) * 100 // blocks
            print(f"\r  {i+1}/{blocks} ({pct}%)", end="", flush=True)
    print()

    # Step 7: cmd=0x39 — apply firmware and reboot
    print("Applying firmware (0x39)...")
    resp = send_recv(dev, DIR_FW, 0, CMD_FW_APPLY, bytes([0x13]))
    dev.close()
    if not resp or resp[4] not in (DIR_FW_ACK, DIR_RESP):
        print("Warning: no ack for apply (device may have rebooted already)")
    else:
        print("Apply acknowledged. Device rebooting (~20s). Then unplug and replug.")
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

    path = devs[0]['path']
    print(f"Found: {devs[0].get('product_string','?')} on {path}")

    if not flash(path, fw_path):
        sys.exit(1)


if __name__ == "__main__":
    main()
