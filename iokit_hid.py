#!/usr/bin/env python3
"""
Direct macOS IOKit HID transport for DSP-408.

This module bypasses hidapi's broken background-thread run loop and instead:
  1. Opens the HID device with kIOHIDOptionsTypeNone (non-exclusive)
  2. Registers an input-report callback on the MAIN THREAD's run loop
  3. Pumps the main run loop explicitly during recv() to deliver callbacks

Requires: macOS only (uses IOKit / CoreFoundation via ctypes)

Verified:
  - ΔSetReportCount/ΔInputReportCount both +N per send/recv pair
    → device IS responding; hidapi just doesn't deliver the reports to Python
  - This module drains the IOKit delivery path correctly
"""

from __future__ import annotations
import ctypes, ctypes.util, sys, time
from typing import Optional

if sys.platform != 'darwin':
    raise ImportError("iokit_hid is macOS only")

# ── Frameworks ────────────────────────────────────────────────────────────────
_IOKit = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
_CF    = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

# ── CoreFoundation types ──────────────────────────────────────────────────────
_CFAllocRef = ctypes.c_void_p
_CFTypeRef  = ctypes.c_void_p
_CFStrRef   = ctypes.c_void_p
_CFNumRef   = ctypes.c_void_p
_CFDictRef  = ctypes.c_void_p
_CFRunRef   = ctypes.c_void_p
_IOReturn   = ctypes.c_int32
_IOHIDRef   = ctypes.c_void_p

# kCFRunLoopDefaultMode (a global CFStringRef)
_kCFRunLoopDefaultMode = ctypes.c_void_p.in_dll(_CF, 'kCFRunLoopDefaultMode')

_CF.CFStringCreateWithCString.restype  = _CFStrRef
_CF.CFStringCreateWithCString.argtypes = [_CFAllocRef, ctypes.c_char_p, ctypes.c_uint32]
_CF.CFNumberCreate.restype  = _CFNumRef
_CF.CFNumberCreate.argtypes = [_CFAllocRef, ctypes.c_uint32, ctypes.c_void_p]
_CF.CFDictionaryCreate.restype  = _CFDictRef
_CF.CFDictionaryCreate.argtypes = [_CFAllocRef,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.c_ssize_t,
                                    ctypes.c_void_p, ctypes.c_void_p]
_CF.CFRelease.restype  = None
_CF.CFRelease.argtypes = [_CFTypeRef]
_CF.CFRunLoopGetCurrent.restype  = _CFRunRef
_CF.CFRunLoopGetCurrent.argtypes = []
_CF.CFRunLoopRunInMode.restype   = ctypes.c_int32
_CF.CFRunLoopRunInMode.argtypes  = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]

# IOKit HID
_IOKit.IORegistryEntryFromPath.restype  = ctypes.c_uint32  # io_registry_entry_t
_IOKit.IORegistryEntryFromPath.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
_IOKit.IOHIDDeviceCreate.restype  = _IOHIDRef
_IOKit.IOHIDDeviceCreate.argtypes = [_CFAllocRef, ctypes.c_uint32]
_IOKit.IOHIDDeviceOpen.restype    = _IOReturn
_IOKit.IOHIDDeviceOpen.argtypes   = [_IOHIDRef, ctypes.c_uint32]
_IOKit.IOHIDDeviceClose.restype   = _IOReturn
_IOKit.IOHIDDeviceClose.argtypes  = [_IOHIDRef, ctypes.c_uint32]
_IOKit.IOHIDDeviceScheduleWithRunLoop.restype  = None
_IOKit.IOHIDDeviceScheduleWithRunLoop.argtypes = [_IOHIDRef, _CFRunRef, ctypes.c_void_p]
_IOKit.IOHIDDeviceUnscheduleFromRunLoop.restype  = None
_IOKit.IOHIDDeviceUnscheduleFromRunLoop.argtypes = [_IOHIDRef, _CFRunRef, ctypes.c_void_p]
_IOKit.IOHIDDeviceSetReport.restype  = _IOReturn
_IOKit.IOHIDDeviceSetReport.argtypes = [_IOHIDRef, ctypes.c_uint32, ctypes.c_ssize_t,
                                         ctypes.c_char_p, ctypes.c_ssize_t]
_IOKit.IOHIDDeviceRegisterInputReportCallback.restype  = None
_IOKit.IOHIDDeviceRegisterInputReportCallback.argtypes = [
    _IOHIDRef,
    ctypes.c_char_p,    # report buffer
    ctypes.c_ssize_t,   # report buffer size
    ctypes.c_void_p,    # callback
    ctypes.c_void_p,    # context
]
_IOKit.IOObjectRelease.restype  = _IOReturn
_IOKit.IOObjectRelease.argtypes = [ctypes.c_uint32]

kCFStringEncodingUTF8  = 0x08000100
kCFNumberSInt32Type    = 3
kIOHIDOptionsTypeNone  = 0x00000000
kIOHIDReportTypeOutput = 1
kIOReturnSuccess       = 0
REPORT_SIZE = 64


class IOKitHIDTransport:
    """
    macOS IOKit HID transport with correct run-loop delivery.

    Usage:
        t = IOKitHIDTransport(hidraw_path_bytes)  # path from hid.enumerate()
        t.open()
        t.send(frame_64_bytes)
        payload = t.recv(timeout_ms=500)
        t.close()
    """

    def __init__(self, path: bytes):
        """
        path: the b'DevSrvsID:NNNNNNNNNN' bytes from hid.enumerate()[0]['path']
        """
        self._path       = path
        self._dev        = None          # IOHIDDeviceRef
        self._run_loop   = None          # CFRunLoopRef (main thread)
        self._report_buf = (ctypes.c_uint8 * REPORT_SIZE)()
        self._inbox: list[bytes] = []
        self._cb_fn      = None          # keep the callback alive

    def open(self) -> None:
        # The hidapi path is b"DevSrvsID:<decimal>" where the decimal is the
        # IOKit REGISTRY ENTRY ID (uint64, NOT the same as io_service_t mach port).
        # To get an actual io_service_t from an entry ID we use:
        #   IOServiceGetMatchingService(kIOMainPortDefault,
        #                               IORegistryEntryIDMatching(entryID))
        entry_id = int(self._path.split(b':')[1])

        # IORegistryEntryIDMatching
        _IOKit.IORegistryEntryIDMatching.restype  = _CFDictRef
        _IOKit.IORegistryEntryIDMatching.argtypes = [ctypes.c_uint64]
        _IOKit.IOServiceGetMatchingService.restype  = ctypes.c_uint32  # io_service_t
        _IOKit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, _CFDictRef]

        matching = _IOKit.IORegistryEntryIDMatching(ctypes.c_uint64(entry_id))
        kIOMainPortDefault = ctypes.c_uint32(0)
        service_t = _IOKit.IOServiceGetMatchingService(kIOMainPortDefault, matching)
        if not service_t:
            raise OSError(f"IOServiceGetMatchingService failed for entry ID {entry_id}")

        # Create IOHIDDeviceRef from service
        kCFAllocatorDefault = ctypes.c_void_p.in_dll(_CF, 'kCFAllocatorDefault')
        dev = _IOKit.IOHIDDeviceCreate(kCFAllocatorDefault, ctypes.c_uint32(service_t))
        if not dev:
            raise OSError(f"IOHIDDeviceCreate failed for service {svc_id:#010x}")

        # Open NON-EXCLUSIVELY
        ret = _IOKit.IOHIDDeviceOpen(dev, kIOHIDOptionsTypeNone)
        if ret != kIOReturnSuccess:
            raise OSError(f"IOHIDDeviceOpen(kIOHIDOptionsTypeNone) failed: {ret:#010x}")

        # Get the CURRENT (main) thread's run loop
        self._run_loop = _CF.CFRunLoopGetCurrent()

        # Schedule the device on the main run loop
        _IOKit.IOHIDDeviceScheduleWithRunLoop(dev, self._run_loop, _kCFRunLoopDefaultMode)

        # Register input callback
        _CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_ssize_t)

        def _callback(ctx, result, sender, report_type_ref, report_len):
            n = min(report_len, REPORT_SIZE)
            data = bytes(self._report_buf[:n])
            self._inbox.append(data)

        cb = _CB(_callback)
        self._cb_fn = cb  # prevent GC

        _IOKit.IOHIDDeviceRegisterInputReportCallback(
            dev,
            ctypes.cast(self._report_buf, ctypes.c_char_p),
            REPORT_SIZE,
            cb,
            None)

        self._dev = dev

    def close(self) -> None:
        if self._dev:
            if self._run_loop:
                _IOKit.IOHIDDeviceUnscheduleFromRunLoop(
                    self._dev, self._run_loop, _kCFRunLoopDefaultMode)
            _IOKit.IOHIDDeviceClose(self._dev, kIOHIDOptionsTypeNone)
            self._dev = None
        self._run_loop = None

    def send(self, frame: bytes) -> None:
        assert len(frame) == REPORT_SIZE, f"frame must be {REPORT_SIZE} bytes"
        ret = _IOKit.IOHIDDeviceSetReport(
            self._dev,
            kIOHIDReportTypeOutput,
            0,              # reportID = 0 (no report IDs)
            frame,
            REPORT_SIZE)
        if ret != kIOReturnSuccess:
            raise OSError(f"IOHIDDeviceSetReport failed: {ret:#010x}")

    def recv(self, timeout_ms: int = 500) -> Optional[bytes]:
        """Pump the run loop until a report arrives or timeout."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Pump the run loop for a short slice.
            # kCFRunLoopRunTimedOut = 3, kCFRunLoopRunHandledSource = 2
            _CF.CFRunLoopRunInMode(_kCFRunLoopDefaultMode, min(remaining, 0.05), True)
            if self._inbox:
                return self._inbox.pop(0)
        return None

    def info(self) -> str:
        return f"IOKitHIDTransport({self._path!r})"


def find_dsp408(vid: int = 0x0483, pid: int = 0x5750) -> Optional[bytes]:
    """Return the hidapi path for the DSP-408, or None if not found."""
    try:
        import hid
        devs = hid.enumerate(vid, pid)
        return devs[0]['path'] if devs else None
    except ImportError:
        return None


if __name__ == '__main__':
    # Quick smoke test
    from dsp408_hid import (
        build_frame, parse_frame, cmd_init, cmd_poll, cmd_firmware,
        OP_POLL, REPORT_SIZE
    )

    path = find_dsp408()
    if path is None:
        print("DSP-408 not found")
        sys.exit(1)

    print(f"Found device at {path!r}")
    t = IOKitHIDTransport(path)
    t.open()
    print("Opened OK (kIOHIDOptionsTypeNone)")

    for label, frame in [
        ('OP_INIT (0x10)', cmd_init()),
        ('OP_POLL (0x40)', cmd_poll()),
        ('OP_FW   (0x13)', cmd_firmware()),
    ]:
        print(f"\n→ Sending {label}...")
        t.send(frame)
        raw = t.recv(timeout_ms=800)
        if raw is None:
            print("  ← (timeout)")
        else:
            payload = parse_frame(raw)
            if payload:
                print(f"  ← OK payload={payload.hex()} ({len(payload)} bytes)")
            else:
                print(f"  ← raw={raw[:16].hex(' ')}... (parse failed)")

    t.close()
    print("\nDone.")
