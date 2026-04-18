#!/usr/bin/env python3
"""
DSP-408 USB transport via PyUSB direct interrupt endpoint access.

WHY THIS EXISTS
===============
On macOS 26 (Darwin 25.x), the DSP-408 is managed by Apple's DriverKit HID
driver (AppleUserUSBHostHIDDevice, com.apple.driverkit.AppleUserHIDDrivers).
This DriverKit driver successfully receives input reports from the device
(confirmed via IORegistry InputReportCount counter), but does NOT forward
them to user-space processes via the traditional IOHIDLibUserClient path.

All standard approaches fail:
  - IOHIDDeviceRegisterInputReportCallback → 0 callbacks (IOKit HID broken)
  - IOHIDDeviceRegisterInputReportWithTimeStampCallback → 0 callbacks
  - IOHIDDeviceRegisterInputValueCallback → 0 callbacks
  - IOHIDDeviceGetReport (sync) → -0x1fffb000 error
  - hidapi blocking read → timeout (uses same broken IOKit path)
  - Native C test program from Terminal.app → also 0 callbacks
  - Both kIOHIDOptionsTypeNone and kIOHIDOptionsTypeSeizeDevice → 0 callbacks

This is confirmed NOT a TCC/Input Monitoring issue (C program from Terminal
also fails). It is an architectural issue in macOS 26's DriverKit HID stack.

SOLUTION
=========
PyUSB uses libusb which can detach the DriverKit driver and claim the USB
interface directly, allowing raw interrupt IN/OUT endpoint transfers.

REQUIREMENT
===========
Root access. The detach_kernel_driver() call requires privileges.

USAGE
=====
    sudo .venv/bin/python3 usb_transport.py            # smoke test
    sudo .venv/bin/python3 -c "
        from usb_transport import USBTransport
        t = USBTransport()
        t.open()
        t.send(frame_64_bytes)
        data = t.recv(timeout_ms=500)
        t.close()
    "
"""

from __future__ import annotations
import time
from typing import Optional

try:
    import usb.core
    import usb.util
except ImportError:
    raise ImportError("PyUSB not found: pip install pyusb")

VID = 0x0483
PID = 0x5750
EP_OUT = 0x01   # Interrupt OUT endpoint
EP_IN  = 0x82   # Interrupt IN endpoint
REPORT_SIZE = 64
INTERFACE = 0


def build_frame(cmd: int, data: list = None) -> bytes:
    """Build DLE/STX framed 64-byte HID packet."""
    payload = [cmd] + (list(data) if data else [])
    length = len(payload)
    chk = length
    for b in payload:
        chk ^= b
    frame = bytes([0x10, 0x02, 0x00, 0x01, length] + payload + [0x10, 0x03, chk])
    return frame.ljust(REPORT_SIZE, b'\x00')


def parse_frame(raw: bytes) -> Optional[tuple]:
    """Parse DLE/STX frame → (cmd, data_bytes) or None if invalid."""
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
        return (payload[0], payload[1:])
    except (ValueError, IndexError):
        return None


class USBTransport:
    """
    Direct USB interrupt endpoint transport for DSP-408.

    Must be opened as root (sudo) so libusb can detach the DriverKit
    HID driver from the USB interface.

    Usage:
        t = USBTransport()
        t.open()            # detaches DriverKit driver, claims interface
        t.send(frame)       # 64-byte raw frame
        data = t.recv()     # 64-byte response or None on timeout
        t.close()           # releases interface, reattaches driver
    """

    def __init__(self, vid: int = VID, pid: int = PID):
        self._vid = vid
        self._pid = pid
        self._dev = None
        self._driver_was_active = False

    def open(self) -> None:
        """Find the device, detach the HID driver, claim the USB interface."""
        dev = usb.core.find(idVendor=self._vid, idProduct=self._pid)
        if dev is None:
            raise OSError(f"USB device {self._vid:#06x}:{self._pid:#06x} not found")

        # Check and detach kernel (DriverKit) driver
        try:
            self._driver_was_active = dev.is_kernel_driver_active(INTERFACE)
        except Exception:
            self._driver_was_active = False

        if self._driver_was_active:
            try:
                dev.detach_kernel_driver(INTERFACE)
            except usb.core.USBError as e:
                raise OSError(
                    f"Cannot detach DriverKit HID driver (need root?): {e}\n"
                    f"Run: sudo .venv/bin/python3 your_script.py"
                ) from e

        # Set configuration (select config 1)
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            # May already be configured
            pass

        # Claim interface 0
        usb.util.claim_interface(dev, INTERFACE)

        self._dev = dev

    def close(self) -> None:
        """Release interface and optionally reattach the HID driver."""
        if self._dev is None:
            return
        try:
            usb.util.release_interface(self._dev, INTERFACE)
            if self._driver_was_active:
                try:
                    self._dev.attach_kernel_driver(INTERFACE)
                except Exception:
                    pass  # best-effort reattach
        except Exception:
            pass
        finally:
            usb.util.dispose_resources(self._dev)
            self._dev = None

    def send(self, frame: bytes) -> None:
        """Write a 64-byte frame to the interrupt OUT endpoint."""
        if len(frame) != REPORT_SIZE:
            frame = frame[:REPORT_SIZE].ljust(REPORT_SIZE, b'\x00')
        n = self._dev.write(EP_OUT, frame, timeout=1000)
        if n != REPORT_SIZE:
            raise OSError(f"Short write: sent {n} of {REPORT_SIZE} bytes")

    def recv(self, timeout_ms: int = 500) -> Optional[bytes]:
        """
        Read one 64-byte input report from the interrupt IN endpoint.
        Returns raw bytes or None on timeout.
        """
        try:
            data = self._dev.read(EP_IN, REPORT_SIZE, timeout=timeout_ms)
            return bytes(data)
        except usb.core.USBTimeoutError:
            return None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    def info(self) -> str:
        if self._dev:
            return (f"USBTransport({self._vid:#06x}:{self._pid:#06x} "
                    f"bus={self._dev.bus} addr={self._dev.address})")
        return f"USBTransport({self._vid:#06x}:{self._pid:#06x} closed)"


if __name__ == '__main__':
    import sys

    print("=== DSP-408 USB Direct Transport Test ===")
    print("(requires root: run with sudo)\n")

    t = USBTransport()
    try:
        t.open()
    except OSError as e:
        print(f"OPEN FAILED: {e}")
        sys.exit(1)

    print(f"Opened: {t.info()}")
    print()

    tests = [
        ("OP_INIT  (0x10)", build_frame(0x10, [0x10])),
        ("OP_FW    (0x13)", build_frame(0x13, [0x13])),
        ("OP_INFO  (0x2c)", build_frame(0x2c, [0x2c])),
        ("OP_POLL  (0x40)", build_frame(0x40, [0x40])),
    ]

    for label, frame in tests:
        print(f"→ Sending {label}...")
        try:
            t.send(frame)
        except Exception as e:
            print(f"  Send failed: {e}")
            continue

        raw = t.recv(timeout_ms=800)
        if raw is None:
            print("  ← (timeout — no response)")
        else:
            parsed = parse_frame(raw)
            if parsed:
                cmd, data = parsed
                print(f"  ← OK  cmd={cmd:#04x}  payload={data.hex()!r}  ({len(data)}B)")
            else:
                print(f"  ← raw={raw[:16].hex(' ')}... (parse failed)")
        print()

    t.close()
    print("Done. Interface released, DriverKit driver reattached.")
