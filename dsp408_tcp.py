#!/usr/bin/env python3
"""
DSP-408 Network Interface (TCP/IP)

The DSP-408 uses TCP/IP for control, not USB HID.
Default IP: 192.168.3.100, Port: typically 5000 or similar

Protocol: DLE/STX framing over TCP
  - STX: 0x10 0x02
  - Sequence: 2 bytes (0x00 0x01 for host->device)
  - Command: 1 byte
  - Data: variable length
  - ETX: 0x10 0x03
  - Checksum: XOR of all bytes from STX to ETX
"""

import socket
import time
from typing import Optional, Tuple, List
from dataclasses import dataclass

DEFAULT_IP = "192.168.3.100"
DEFAULT_PORT = 5000
TIMEOUT = 1.0

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
    """Build DLE/STX framed packet with checksum."""
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
    device_name: str


class DSP408:
    def __init__(self, ip: str = DEFAULT_IP, port: int = DEFAULT_PORT):
        self.ip = ip
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self._buffer = b""

    def connect(self, timeout: float = 2.0) -> bool:
        """Connect to DSP-408 via TCP."""
        print(f"Connecting to {self.ip}:{self.port}...")

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect((self.ip, self.port))
            self.sock.setblocking(False)
            self.connected = True

            # Drain any pending data
            time.sleep(0.1)
            self._drain()

            print("  Connected OK")
            return True

        except socket.timeout:
            print(f"  Connection timeout")
        except ConnectionRefusedError:
            print(f"  Connection refused")
        except OSError as e:
            print(f"  Error: {e}")

        return False

    def disconnect(self):
        """Close connection."""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        self.connected = False

    def _drain(self, timeout: float = 0.5):
        """Drain any pending data from socket."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                self._buffer += data
            except BlockingIOError:
                break
            except:
                break

    def _send_raw(self, payload: bytes) -> bool:
        """Send raw bytes."""
        try:
            self.sock.sendall(payload)
            return True
        except:
            return False

    def _receive_frame(self, timeout: float = TIMEOUT) -> Optional[List[int]]:
        """Receive and parse a single frame."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                data = self.sock.recv(4096)
                if data:
                    self._buffer += data

                    # Try to parse frame
                    parsed = parse_frame(self._buffer)
                    if parsed:
                        # Remove parsed frame from buffer
                        etx_idx = self._buffer.index(bytes([0x10, 0x03]))
                        self._buffer = self._buffer[
                            etx_idx + 3 :
                        ]  # +3 for ETX + checksum
                        return parsed[0]

            except BlockingIOError:
                time.sleep(0.01)
            except:
                break

        return None

    def _send_command(
        self, cmd: int, data: List[int] = None, timeout: float = TIMEOUT
    ) -> Optional[List[int]]:
        """Send framed command and parse response."""
        payload = data or []
        frame = build_frame(0x00, 0x01, cmd, payload)

        if not self._send_raw(frame):
            return None

        return self._receive_frame(timeout)

    def handshake(self) -> bool:
        """Perform initial handshake."""
        print("Performing handshake...")
        frame = build_frame(0x00, 0x01, CMD_HANDSHAKE, [0x01])

        if not self._send_raw(frame):
            print("  Handshake failed (send error)")
            return False

        response = self._receive_frame()
        if response:
            print(f"  Handshake OK: {bytes(response).hex()}")
            return True

        print("  Handshake failed (no response)")
        return False

    def get_device_info(self) -> Optional[DeviceInfo]:
        """Query device information."""
        print("Querying device info...")
        response = self._send_command(CMD_DEVICE_INFO)

        if response and len(response) >= 2:
            # Response format: [length, cmd, name_bytes...]
            name_bytes = bytes(response[2:])
            try:
                device_name = name_bytes.decode("ascii").strip()
            except:
                device_name = name_bytes.hex()

            version = (
                f"{response[0]}.{response[1]:02d}" if len(response) >= 2 else "unknown"
            )
            print(f"  Device: {device_name}, Version: {version}")
            return DeviceInfo(firmware_version=version, device_name=device_name)

        print("  Failed to get device info")
        return None

    def get_status(self) -> Optional[List[int]]:
        """Get current device status."""
        print("Getting status...")
        response = self._send_command(CMD_STATUS)
        if response:
            print(f"  Status: {bytes(response).hex()}")
        return response

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

    def get_preset(self, preset_num: int) -> Optional[str]:
        """Get preset name (0-19)."""
        print(f"Getting preset {preset_num}...")
        response = self._send_command(CMD_GET_PRESET, [preset_num])
        if response and len(response) > 2:
            name_bytes = bytes(response[2:])
            try:
                name = name_bytes.decode("ascii").strip()
                print(f"  Preset {preset_num}: {name!r}")
                return name
            except:
                pass
        return None

    def config_dump(self, chunk: int) -> Optional[List[int]]:
        """Get config dump chunk (0-28)."""
        response = self._send_command(CMD_CONFIG_DUMP, [chunk])
        return response

    def keepalive(self) -> Optional[List[int]]:
        """Send keepalive, get meter levels."""
        response = self._send_command(CMD_KEEPALIVE)
        return response

    def listen(self, duration: float = 2.0) -> List[bytes]:
        """Listen for spontaneous messages."""
        messages = []
        deadline = time.time() + duration
        print(f"Listening for {duration}s...")

        while time.time() < deadline:
            try:
                data = self.sock.recv(4096)
                if data:
                    self._buffer += data
                    print(f"  RX: {data.hex()}")
                    messages.append(data)
                time.sleep(0.01)
            except BlockingIOError:
                time.sleep(0.01)
            except:
                break

        return messages


def scan_for_device() -> Optional[Tuple[str, int]]:
    """Scan common ports for DSP-408."""
    common_ports = [5000, 80, 8080, 23, 5001, 5002, 10000]

    for port in common_ports:
        print(f"Scanning port {port}...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((DEFAULT_IP, port))
            sock.close()

            if result == 0:
                print(f"  Port {port} OPEN")
                return (DEFAULT_IP, port)
        except:
            pass

    return None


def main():
    print("=== DSP-408 TCP Interface ===\n")

    # Try to find device
    print("1. Scanning for device...")
    result = scan_for_device()

    if not result:
        print(f"\nDevice not found at {DEFAULT_IP}")
        print("Make sure the DSP-408 is connected to your network")
        print("and your computer is on the same subnet.")
        return

    ip, port = result
    print(f"\nDevice found at {ip}:{port}\n")

    dsp = DSP408(ip, port)

    if not dsp.connect():
        return

    try:
        print("\n--- Testing DSP-408 Connection ---\n")

        # Listen for spontaneous traffic
        print("2. Listening for spontaneous traffic...")
        dsp.listen(1.0)

        # Handshake
        print("\n3. Handshake...")
        if not dsp.handshake():
            print("Handshake failed, but continuing...")

        # Device info
        print("\n4. Device Info...")
        info = dsp.get_device_info()

        # Status
        print("\n5. Status...")
        dsp.get_status()

        # Preset count
        print("\n6. Preset Count...")
        dsp.get_preset_count()

        # Get first few presets
        print("\n7. Get Presets (0-4)...")
        for i in range(5):
            dsp.get_preset(i)

        # Keepalive
        print("\n8. Keepalive...")
        response = dsp.keepalive()
        if response:
            print(f"  Keepalive OK: {len(response)} bytes")

        print("\n--- Tests Complete ---")

    finally:
        dsp.disconnect()


if __name__ == "__main__":
    main()
