#!/usr/bin/env python3
"""DSP-408 USB HID interface with proper initialization sequence."""

import hid
import time
from typing import Optional, List

VID = 0x0483
PID = 0x5750


def build_frame(src: int, dst: int, cmd: int, data: List[int] = None) -> bytes:
    """Build DLE/STX framed packet with checksum."""
    payload = [cmd] + (data or [])
    length = len(payload)
    checksum = length
    for b in payload:
        checksum ^= b

    frame = [0x10, 0x02, src, dst, length] + payload + [0x10, 0x03, checksum]
    return bytes(frame)


def parse_frame(raw: bytes) -> Optional[tuple]:
    """Parse DLE/STX frame, return (cmd, data) or None if invalid."""
    if len(raw) < 7:
        return None

    try:
        stx_idx = raw.index(0x10)
        if raw[stx_idx + 1] != 0x02:
            return None

        length = raw[stx_idx + 4]
        etx_idx = stx_idx + 5 + length

        if raw[etx_idx : etx_idx + 2] != bytes([0x10, 0x03]):
            return None

        data = raw[stx_idx + 5 : etx_idx]
        checksum = raw[etx_idx + 2]

        calc_checksum = length
        for b in data:
            calc_checksum ^= b

        if checksum != calc_checksum:
            return None

        cmd = data[0]
        return (cmd, data[1:])
    except (ValueError, IndexError):
        return None


class DSP408USB:
    def __init__(self):
        self.dev = None
        self.initialized = False

    def connect(self) -> bool:
        """Connect and initialize the device."""
        try:
            self.dev = hid.device()
            self.dev.open(VID, PID)
            print(
                f"Connected: {self.dev.get_manufacturer_string()} - {self.dev.get_product_string()}"
            )

            # Run initialization sequence
            return self._initialize()
        except Exception as e:
            print(f"Failed to connect: {e}")
            return False

    def _initialize(self) -> bool:
        """Run the initialization sequence from miniDSP-Linux."""
        print("Running initialization sequence...")

        # Step 1: 0x10 Init handshake
        print("  1. Sending 0x10 handshake...")
        frame = build_frame(0x00, 0x01, 0x10, [0x10])
        response = self._send_raw(frame)
        if not response:
            print("    FAILED - no response")
            return False
        parsed = parse_frame(response)
        print(f"    Response: {parsed}")

        # Step 2: 0x13 Firmware/model string
        print("  2. Sending 0x13 firmware query...")
        frame = build_frame(0x00, 0x01, 0x13, [0x13])
        response = self._send_raw(frame)
        if not response:
            print("    FAILED - no response")
            return False
        parsed = parse_frame(response)
        if parsed:
            try:
                fw_string = bytes(parsed[1]).decode("ascii", errors="ignore")
                print(f"    Firmware: {fw_string}")
            except:
                print(f"    Response: {parsed}")

        # Step 3: 0x2c Device info
        print("  3. Sending 0x2c device info...")
        frame = build_frame(0x00, 0x01, 0x2C, [0x2C])
        response = self._send_raw(frame)
        if not response:
            print("    FAILED - no response")
            return False
        parsed = parse_frame(response)
        print(f"    Response: {parsed}")

        # Step 4: 0x12 Activate
        print("  4. Sending 0x12 activate...")
        frame = build_frame(0x00, 0x01, 0x12, [0x12])
        response = self._send_raw(frame)
        if not response:
            print("    FAILED - no response")
            return False
        parsed = parse_frame(response)
        print(f"    Response: {parsed}")

        self.initialized = True
        print("Initialization complete!")
        return True

    def _send_raw(self, payload: bytes, timeout: float = 0.5) -> bytes:
        """Send raw 64-byte HID report."""
        # Pad to 64 bytes (no report ID prefix for Linux-style)
        buf = payload[:64].ljust(64, b"\x00")
        self.dev.write(buf)

        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.dev.read(64)
            if data:
                return bytes(data)
            time.sleep(0.01)
        return b""

    def send_command(self, cmd: int, data: List[int] = None) -> Optional[List[int]]:
        """Send a command and get response."""
        if not self.initialized:
            print("Device not initialized!")
            return None

        frame = build_frame(0x00, 0x01, cmd, data or [])
        raw = self._send_raw(frame)
        if not raw:
            return None
        parsed = parse_frame(raw)
        return parsed[1] if parsed else None

    def test_connection(self):
        """Test with 0x40 keepalive/level poll."""
        print("\nTesting connection with 0x40 poll...")
        response = self.send_command(0x40, [0x40])
        if response:
            print(f"SUCCESS! Response: {response.hex()}")
            return True
        else:
            print("FAILED - no response")
            return False

    def close(self):
        if self.dev:
            self.dev.close()


if __name__ == "__main__":
    dsp = DSP408USB()

    if dsp.connect():
        print("\n✓ Device connected and initialized!")
        dsp.test_connection()
        dsp.close()
    else:
        print("\n✗ Failed to connect")
