#!/usr/bin/env python3
"""
DSP-408 USB HID Interface

Protocol: DLE/STX framing over 64-byte HID reports
  - STX: 0x10 0x02
  - Sequence: 2 bytes (0x00 0x01 for host->device, 0x01 0x00 for device->host)
  - Command: 1 byte
  - Data: variable length
  - ETX: 0x10 0x03
  - Checksum: XOR of all bytes from STX to ETX

VID/PID: 0x0483 / 0x5750 (STMicroelectronics)
Endpoints: EP 0x82 (IN), EP 0x01 (OUT), 64-byte interrupt transfers
"""

import hid
import time
from typing import Optional, Tuple, List
from dataclasses import dataclass

VID = 0x0483
PID = 0x5750
PKT_SIZE = 64
TIMEOUT = 0.5

# Command codes (from protocol capture)
CMD_HANDSHAKE = 0x10
CMD_DEVICE_INFO = 0x13
CMD_STATUS = 0x12
CMD_KEEPALIVE = 0x40
CMD_PRESET_CNT = 0x2C
CMD_GET_PRESET = 0x29
CMD_CONFIG_DUMP = 0x27
CMD_CONFIG_DATA = 0x24


def build_frame(seq_hi: int, seq_lo: int, cmd: int, data: List[int] = None) -> bytes:
    """Build DLE/STX framed packet with checksum.

    Format: 10 02 [seq_hi] [seq_lo] [cmd] [data...] 10 03 [checksum]
    """
    stx = [0x10, 0x02]
    payload = [seq_hi, seq_lo, cmd] + (data or [])
    etx = [0x10, 0x03]
    frame = stx + payload + etx
    checksum = 0
    for b in frame:
        checksum ^= b
    return bytes(frame + [checksum])


def parse_frame(raw: bytes) -> Optional[Tuple[List[int], int]]:
    """Parse DLE/STX frame, return (data, checksum) or None if invalid."""
    if len(raw) < 6 or raw[0:2] != bytes([0x10, 0x02]):
        return None

    try:
        etx_idx = raw.index(bytes([0x10, 0x03]))
    except ValueError:
        return None

    frame = raw[: etx_idx + 2]
    received_checksum = raw[etx_idx + 2] if etx_idx + 2 < len(raw) else 0

    calc_checksum = 0
    for b in frame:
        calc_checksum ^= b

    data = list(raw[2:etx_idx])
    return (data, received_checksum) if calc_checksum == received_checksum else None


@dataclass
class DeviceInfo:
    firmware_version: str
    device_type: str
    serial_number: str


class DSP408:
    def __init__(self):
        self.dev: Optional[hid.device] = None
        self.connected = False

    def connect(self) -> bool:
        """Connect to DSP-408 device."""
        print(f"Searching for DSP-408 (VID={VID:#06x}, PID={PID:#06x})...")
        devices = hid.enumerate(VID, PID)

        if not devices:
            print("Device not found. Check USB connection.")
            return False

        d = devices[0]
        print(f"Found: {d['product_string']!r} S/N={d['serial_number']!r}")

        self.dev = hid.device()
        self.dev.open_path(d["path"])
        self.dev.set_nonblocking(True)
        self.connected = True

        # Drain any pending data
        time.sleep(0.1)
        while self.dev.read(PKT_SIZE):
            time.sleep(0.01)

        return True

    def disconnect(self):
        """Close connection."""
        if self.dev:
            self.dev.close()
            self.dev = None
        self.connected = False

    def _send_raw(self, payload: bytes, timeout: float = TIMEOUT) -> bytes:
        """Send raw 64-byte HID report."""
        buf = bytes([0x00]) + payload[: PKT_SIZE - 1]
        self.dev.write(buf)

        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.dev.read(PKT_SIZE)
            if data:
                return bytes(data)
            time.sleep(0.01)
        return b""

    def _send_command(
        self, cmd: int, data: List[int] = None, timeout: float = TIMEOUT
    ) -> Optional[List[int]]:
        """Send framed command and parse response."""
        payload = data or []
        frame = build_frame(0x00, 0x01, cmd, payload)

        raw_response = self._send_raw(frame, timeout)
        if not raw_response:
            return None

        parsed = parse_frame(raw_response)
        return parsed[0] if parsed else None

    def handshake(self) -> bool:
        """Perform initial handshake: 10 02 00 01 01 10 10 03 11"""
        print("Performing handshake...")
        frame = build_frame(0x00, 0x01, CMD_HANDSHAKE, [0x01])
        raw_response = self._send_raw(frame)
        if not raw_response:
            print("  Handshake failed (no response)")
            return False

        parsed = parse_frame(raw_response)
        if parsed:
            print(f"  Handshake OK: {bytes(parsed[0]).hex()}")
            return True
        print("  Handshake failed (invalid frame)")
        return False

    def get_device_info(self) -> Optional[DeviceInfo]:
        """Query device information."""
        print("Querying device info...")
        response = self._send_command(CMD_DEVICE_INFO)
        if response and len(response) >= 2:
            print(f"  Device info: {bytes(response).hex()}")
            return DeviceInfo(
                firmware_version=f"{response[0]}.{response[1]:02d}",
                device_type="DSP-408",
                serial_number="unknown",
            )
        print("  Failed to get device info")
        return None

    def get_status(self) -> Optional[List[int]]:
        """Get current device status."""
        print("Getting status...")
        response = self._send_command(CMD_STATUS)
        if response:
            print(f"  Status: {bytes(response).hex()}")
        return response

    def keepalive(self) -> bool:
        """Send keepalive packet."""
        response = self._send_command(CMD_KEEPALIVE, timeout=0.1)
        return response is not None

    def get_preset_count(self) -> Optional[int]:
        """Query number of presets."""
        print("Querying preset count...")
        response = self._send_command(CMD_PRESET_CNT)
        if response and len(response) >= 1:
            count = response[0]
            print(f"  Preset count: {count}")
            return count
        print("  Failed to get preset count")
        return None

    def get_preset(self, preset_num: int) -> Optional[List[int]]:
        """Get preset data (0-19)."""
        print(f"Getting preset {preset_num}...")
        response = self._send_command(CMD_GET_PRESET, [preset_num])
        if response:
            print(f"  Preset {preset_num}: {len(response)} bytes")
        return response

    def config_dump(self, chunk: int) -> Optional[List[int]]:
        """Get config dump chunk (0-28)."""
        response = self._send_command(CMD_CONFIG_DUMP, [chunk])
        return response

    def listen(self, duration: float = 2.0) -> List[bytes]:
        """Listen for spontaneous messages."""
        messages = []
        deadline = time.time() + duration
        print(f"Listening for {duration}s...")

        while time.time() < deadline:
            data = self.dev.read(PKT_SIZE)
            if data:
                raw = bytes(data)
                print(f"  RX: {raw.hex()}")
                messages.append(raw)
            time.sleep(0.01)

        return messages


def main():
    dsp = DSP408()

    if not dsp.connect():
        return

    try:
        print("\n--- Testing DSP-408 Connection ---\n")

        # Listen for spontaneous traffic
        print("1. Listening for spontaneous traffic...")
        dsp.listen(1.0)

        # Handshake
        print("\n2. Handshake...")
        if not dsp.handshake():
            print("Handshake failed, but continuing...")

        # Device info
        print("\n3. Device Info...")
        info = dsp.get_device_info()
        if info:
            print(f"  Firmware: {info.firmware_version}")

        # Status
        print("\n4. Status...")
        dsp.get_status()

        # Preset count
        print("\n5. Preset Count...")
        dsp.get_preset_count()

        # Keepalive
        print("\n6. Keepalive...")
        print(f"  Keepalive OK: {dsp.keepalive()}")

        print("\n--- Tests Complete ---")

    finally:
        dsp.disconnect()


if __name__ == "__main__":
    main()
