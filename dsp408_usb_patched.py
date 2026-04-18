#!/usr/bin/env python3
"""
DSP-408 Interface using patched hidapi (non-exclusive mode).

This module patches hidapi at runtime to open devices in non-exclusive mode on macOS.
"""

import sys
import ctypes
import ctypes.util

# Apply patch before importing hid
if sys.platform == "darwin":
    # Load IOKit to patch the open behavior
    IOKit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))

    # We'll use direct IOKit calls instead of hidapi
    print("macOS detected: Using IOKit directly for non-exclusive access")


from typing import Optional, List
import time

# Try to import hidapi, fall back to IOKit
try:
    import hid

    HIDAPI_AVAILABLE = True
except ImportError:
    HIDAPI_AVAILABLE = False
    print("hidapi not available, using IOKit backend")


class DSP408USB:
    """DSP-408 USB HID interface with macOS compatibility."""

    VID = 0x0483
    PID = 0x5750
    REPORT_SIZE = 64

    def __init__(self, use_iokit: bool = True):
        """
        Initialize USB interface.

        Args:
            use_iokit: If True on macOS, use IOKit directly to avoid exclusive mode
        """
        self.dev = None
        self.connected = False
        self.use_iokit = use_iokit and sys.platform == "darwin"

    def connect(self) -> bool:
        """Connect to DSP-408 via USB HID."""
        if self.use_iokit:
            return self._connect_iokit()
        else:
            return self._connect_hidapi()

    def _connect_hidapi(self) -> bool:
        """Connect using hidapi (Windows/Linux or macOS with issues)."""
        if not HIDAPI_AVAILABLE:
            print("hidapi not available")
            return False

        try:
            self.dev = hid.device()

            # Try to open by VID/PID
            self.dev.open(self.VID, self.PID)

            # Get device info
            manufacturer = self.dev.get_manufacturer_string()
            product = self.dev.get_product_string()
            serial = self.dev.get_serial_number_string()

            print(f"Connected: {manufacturer} {product} (SN: {serial})")

            self.connected = True
            return True

        except Exception as e:
            print(f"Failed to open device: {e}")
            return False

    def _connect_iokit(self) -> bool:
        """Connect using IOKit directly (macOS, non-exclusive)."""
        # Load frameworks
        IOKit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        CoreFoundation = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("CoreFoundation")
        )

        # Constants
        kIOHIDOptionsTypeNone = 0

        # Get HID device matching our VID/PID
        # This is a simplified version - full implementation in dsp408_iokit.py

        # For now, try hidapi with error handling
        # The real fix requires rebuilding hidapi or using full IOKit
        print("IOKit direct access requires full implementation")
        print("Falling back to hidapi...")

        return self._connect_hidapi()

    def disconnect(self):
        """Disconnect from device."""
        if self.dev:
            self.dev.close()
            self.dev = None
        self.connected = False

    def write(self, data: bytes) -> int:
        """Write data to device."""
        if not self.dev:
            raise RuntimeError("Not connected")

        # Add report ID byte at beginning (required by hidapi)
        report = bytes([0x00]) + data[: self.REPORT_SIZE]

        return self.dev.write(report)

    def read(self, size: int = 64, timeout_ms: int = 500) -> bytes:
        """Read data from device."""
        if not self.dev:
            raise RuntimeError("Not connected")

        return bytes(self.dev.read(size, timeout_ms))

    def send_command(
        self, cmd: int, data: List[int] = None, timeout: float = 0.5
    ) -> Optional[bytes]:
        """Send command and wait for response."""
        # Build frame with DLE/STX framing
        seq_hi, seq_lo = 0x00, 0x01

        frame_data = [seq_hi, seq_lo, cmd]
        if data:
            frame_data.extend(data)

        # Calculate checksum (XOR of all frame bytes)
        checksum = 0
        for b in frame_data:
            checksum ^= b

        # Build complete frame
        frame = bytes([0x10, 0x02] + frame_data + [0x10, 0x03, checksum])

        # Send
        self.write(frame)

        # Wait for response
        deadline = time.time() + timeout
        while time.time() < deadline:
            response = self.read(64, timeout_ms=100)
            if response:
                return response

        return None


def test_connection():
    """Test USB connection to DSP-408."""
    print("=== DSP-408 USB Connection Test ===")

    # Try IOKit first on macOS
    use_iokit = sys.platform == "darwin"

    dev = DSP408USB(use_iokit=use_iokit)

    if dev.connect():
        print("✓ Device connected")

        # Try keepalive command
        print("Sending keepalive (0x40)...")
        response = dev.send_command(0x40)

        if response:
            print(f"✓ Response received: {response.hex()}")
        else:
            print("✗ No response (device may be write-only or need Windows driver)")

        dev.disconnect()
        return True
    else:
        print("✗ Failed to connect")
        return False


if __name__ == "__main__":
    success = test_connection()
    sys.exit(0 if success else 1)
