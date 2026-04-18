#!/usr/bin/env python3
"""
DSP-408 Python Interface

The DSP-408 can be controlled via:
1. USB HID (VID=0x0483, PID=0x5750) - requires macOS permissions
2. TCP/IP Network (default 192.168.3.100:5000) - preferred method

Usage:
    python dsp408_interface.py          # Auto-detect connection
    python dsp408_interface.py --usb    # Force USB HID
    python dsp408_interface.py --tcp    # Force TCP/IP
    python dsp408_interface.py --ip 192.168.3.100 --port 5000
"""

import sys
import time
from typing import Optional, List, Tuple
from dataclasses import dataclass

# Try to import dependencies
try:
    import hid

    HID_AVAILABLE = True
except ImportError:
    HID_AVAILABLE = False

try:
    import socket

    SOCKET_AVAILABLE = True
except ImportError:
    SOCKET_AVAILABLE = False


# ============================================================================
# Protocol Constants
# ============================================================================

VID = 0x0483
PID = 0x5750
PKT_SIZE = 64

DEFAULT_IP = "192.168.3.100"
DEFAULT_PORT = 5000

# Command codes (from protocol analysis)
CMD_HANDSHAKE = 0x10
CMD_DEVICE_INFO = 0x13
CMD_STATUS = 0x12
CMD_KEEPALIVE = 0x40
CMD_PRESET_CNT = 0x2C
CMD_GET_PRESET = 0x29
CMD_CONFIG_DUMP = 0x27
CMD_CONFIG_DATA = 0x24
CMD_SET_GAIN = 0x34
CMD_SET_MUTE = 0x35
CMD_SET_GEQ = 0x48
CMD_SET_PEQ = 0x33
CMD_SET_HPF = 0x32
CMD_SET_LPF = 0x31
CMD_SET_MATRIX = 0x3A


# ============================================================================
# Protocol Framing
# ============================================================================


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
    device_name: str
    serial_number: str = ""


# ============================================================================
# USB HID Interface
# ============================================================================


class DSP408_USB:
    """DSP-408 USB HID interface."""

    def __init__(self):
        self.dev = None
        self.connected = False

    def connect(self) -> bool:
        if not HID_AVAILABLE:
            print("hidapi not available. Install: pip install hidapi")
            return False

        print(f"Searching for DSP-408 USB (VID={VID:#06x}, PID={PID:#06x})...")
        devices = hid.enumerate(VID, PID)

        if not devices:
            print("Device not found. Check USB connection.")
            return False

        d = devices[0]
        print(f"Found: {d['product_string']!r} S/N={d['serial_number']!r}")

        try:
            self.dev = hid.device()
            self.dev.open_path(d["path"])
            self.dev.set_nonblocking(True)
            self.connected = True

            # Drain pending data
            time.sleep(0.1)
            while self.dev.read(PKT_SIZE):
                time.sleep(0.01)

            return True
        except Exception as e:
            print(f"Failed to open device: {e}")
            print("macOS: May require Input Monitoring permission")
            return False

    def disconnect(self):
        if self.dev:
            self.dev.close()
            self.dev = None
        self.connected = False

    def _send_raw(self, payload: bytes, timeout: float = 0.5) -> bytes:
        buf = bytes([0x00]) + payload[:PKT_SIZE]
        self.dev.write(buf)

        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.dev.read(PKT_SIZE)
            if data:
                return bytes(data)
            time.sleep(0.01)
        return b""

    def _send_command(
        self, cmd: int, data: List[int] = None, timeout: float = 0.5
    ) -> Optional[List[int]]:
        frame = build_frame(0x00, 0x01, cmd, data or [])
        raw = self._send_raw(frame, timeout)
        if not raw:
            return None
        parsed = parse_frame(raw)
        return parsed[0] if parsed else None

    def handshake(self) -> bool:
        frame = build_frame(0x00, 0x01, CMD_HANDSHAKE, [0x01])
        raw = self._send_raw(frame)
        parsed = parse_frame(raw)
        return parsed is not None

    def get_device_info(self) -> Optional[DeviceInfo]:
        response = self._send_command(CMD_DEVICE_INFO)
        if response and len(response) >= 2:
            return DeviceInfo(
                firmware_version=f"{response[0]}.{response[1]:02d}",
                device_name="DSP-408",
                serial_number="",
            )
        return None

    def get_preset_count(self) -> Optional[int]:
        response = self._send_command(CMD_PRESET_CNT)
        return response[0] if response and len(response) >= 1 else None

    def get_preset(self, preset_num: int) -> Optional[str]:
        response = self._send_command(CMD_GET_PRESET, [preset_num])
        if response and len(response) > 2:
            try:
                return bytes(response[2:]).decode("ascii").strip()
            except:
                pass
        return None

    def keepalive(self) -> Optional[List[int]]:
        return self._send_command(CMD_KEEPALIVE)


# ============================================================================
# TCP/IP Interface
# ============================================================================


class DSP408_TCP:
    """DSP-408 TCP/IP network interface."""

    def __init__(self, ip: str = DEFAULT_IP, port: int = DEFAULT_PORT):
        self.ip = ip
        self.port = port
        self.sock = None
        self.connected = False
        self._buffer = b""

    def connect(self, timeout: float = 2.0) -> bool:
        if not SOCKET_AVAILABLE:
            print("socket not available")
            return False

        print(f"Connecting to {self.ip}:{self.port}...")

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect((self.ip, self.port))
            self.sock.setblocking(False)
            self.connected = True

            time.sleep(0.1)
            self._drain()

            print("  Connected OK")
            return True
        except Exception as e:
            print(f"  Connection failed: {e}")
            return False

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        self.connected = False

    def _drain(self, timeout: float = 0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                self._buffer += data
            except:
                break

    def _send_raw(self, payload: bytes) -> bool:
        try:
            self.sock.sendall(payload)
            return True
        except:
            return False

    def _receive_frame(self, timeout: float = 1.0) -> Optional[List[int]]:
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                data = self.sock.recv(4096)
                if data:
                    self._buffer += data
                    parsed = parse_frame(self._buffer)
                    if parsed:
                        etx_idx = self._buffer.index(bytes([0x10, 0x03]))
                        self._buffer = self._buffer[etx_idx + 3 :]
                        return parsed[0]
            except:
                time.sleep(0.01)

        return None

    def _send_command(
        self, cmd: int, data: List[int] = None, timeout: float = 1.0
    ) -> Optional[List[int]]:
        frame = build_frame(0x00, 0x01, cmd, data or [])
        if not self._send_raw(frame):
            return None
        return self._receive_frame(timeout)

    def handshake(self) -> bool:
        frame = build_frame(0x00, 0x01, CMD_HANDSHAKE, [0x01])
        if not self._send_raw(frame):
            return False
        response = self._receive_frame()
        return response is not None

    def get_device_info(self) -> Optional[DeviceInfo]:
        response = self._send_command(CMD_DEVICE_INFO)
        if response and len(response) >= 2:
            name_bytes = bytes(response[2:])
            try:
                device_name = name_bytes.decode("ascii").strip()
            except:
                device_name = name_bytes.hex()
            return DeviceInfo(
                firmware_version=f"{response[0]}.{response[1]:02d}",
                device_name=device_name,
                serial_number="",
            )
        return None

    def get_preset_count(self) -> Optional[int]:
        response = self._send_command(CMD_PRESET_CNT)
        return response[0] if response and len(response) >= 1 else None

    def get_preset(self, preset_num: int) -> Optional[str]:
        response = self._send_command(CMD_GET_PRESET, [preset_num])
        if response and len(response) > 2:
            try:
                return bytes(response[2:]).decode("ascii").strip()
            except:
                pass
        return None

    def keepalive(self) -> Optional[List[int]]:
        return self._send_command(CMD_KEEPALIVE)


# ============================================================================
# Unified Interface
# ============================================================================


class DSP408:
    """Unified DSP-408 interface (auto-detects USB or TCP)."""

    def __init__(
        self,
        ip: str = None,
        port: int = None,
        force_usb: bool = False,
        force_tcp: bool = False,
    ):
        self.usb = DSP408_USB()
        self.tcp = DSP408_TCP(ip or DEFAULT_IP, port or DEFAULT_PORT)
        self._impl = None
        self.force_usb = force_usb
        self.force_tcp = force_tcp

    def connect(self) -> bool:
        """Connect via USB or TCP (auto-detect or forced)."""
        if self.force_usb:
            print("Forcing USB connection...")
            if self.usb.connect():
                self._impl = self.usb
                return True
            return False

        if self.force_tcp:
            print("Forcing TCP connection...")
            if self.tcp.connect():
                self._impl = self.tcp
                return True
            return False

        # Auto-detect: try USB first, then TCP
        print("Auto-detecting connection method...")

        if HID_AVAILABLE:
            print("\nTrying USB...")
            if self.usb.connect():
                print("Connected via USB\n")
                self._impl = self.usb
                return True

        print("\nTrying TCP/IP...")
        if self.tcp.connect():
            print("Connected via TCP/IP\n")
            self._impl = self.tcp
            return True

        print("\nNo connection method succeeded.")
        return False

    def disconnect(self):
        if self._impl:
            self._impl.disconnect()
            self._impl = None

    # Delegate all methods to active implementation
    def __getattr__(self, name):
        if self._impl:
            return getattr(self._impl, name)
        raise AttributeError("Not connected")


# ============================================================================
# CLI
# ============================================================================


def main():
    import argparse

    parser = argparse.ArgumentParser(description="DSP-408 Control Interface")
    parser.add_argument("--usb", action="store_true", help="Force USB HID connection")
    parser.add_argument("--tcp", action="store_true", help="Force TCP/IP connection")
    parser.add_argument(
        "--ip", default=DEFAULT_IP, help=f"TCP IP address (default: {DEFAULT_IP})"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TCP port (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()

    print("=" * 50)
    print("DSP-408 Python Interface")
    print("=" * 50)

    dsp = DSP408(ip=args.ip, port=args.port, force_usb=args.usb, force_tcp=args.tcp)

    if not dsp.connect():
        print("\nFailed to connect. Check:")
        print("  - USB: Device plugged in, macOS permissions (Input Monitoring)")
        print(f"  - TCP: Device at {args.ip}:{args.port}, same network subnet")
        sys.exit(1)

    try:
        print("Testing connection...")
        print(f"  Handshake: {'OK' if dsp.handshake() else 'FAILED'}")

        info = dsp.get_device_info()
        if info:
            print(f"  Device: {info.device_name}")
            print(f"  Firmware: {info.firmware_version}")

        count = dsp.get_preset_count()
        print(f"  Presets: {count}" if count else "  Presets: unknown")

        print("\n  First 5 presets:")
        for i in range(5):
            name = dsp.get_preset(i)
            print(f"    U{i + 1:02d}: {name or '(empty)'}")

        print("\n  Keepalive:", "OK" if dsp.keepalive() else "FAILED")

        print("\n" + "=" * 50)
        print("Connection test complete!")
        print("=" * 50)

    finally:
        dsp.disconnect()


if __name__ == "__main__":
    main()
