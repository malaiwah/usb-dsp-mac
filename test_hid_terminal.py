#!/usr/bin/env python3
"""
Run this directly from Terminal.app (NOT from Claude Code).
Terminal.app should have Input Monitoring permission in:
  System Settings → Privacy & Security → Input Monitoring

This tests whether IOKit HID input report callbacks work when the
responsible process is Terminal.app (which has Input Monitoring).

Usage:
  cd /Users/mbelleau/Code/usb_dsp_mac
  .venv/bin/python3 test_hid_terminal.py
"""
import ctypes, sys, time

IOKit = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
CF    = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

_CFAllocRef = ctypes.c_void_p
_CFDictRef  = ctypes.c_void_p
_IOHIDRef   = ctypes.c_void_p
_IOReturn   = ctypes.c_int32
_CFIndex    = ctypes.c_ssize_t

kCFAllocatorDefault   = ctypes.c_void_p.in_dll(CF, 'kCFAllocatorDefault')
kCFRunLoopDefaultMode = ctypes.c_void_p.in_dll(CF, 'kCFRunLoopDefaultMode')
kIOHIDOptionsTypeNone  = 0
kIOHIDReportTypeOutput = 1
kIOReturnSuccess       = 0
REPORT_SIZE = 64

# CoreFoundation
CF.CFRunLoopGetCurrent.restype  = ctypes.c_void_p
CF.CFRunLoopGetCurrent.argtypes = []
CF.CFRunLoopRunInMode.restype   = ctypes.c_int32
CF.CFRunLoopRunInMode.argtypes  = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]

# IOKit HID
IOKit.IORegistryEntryIDMatching.restype  = _CFDictRef
IOKit.IORegistryEntryIDMatching.argtypes = [ctypes.c_uint64]
IOKit.IOServiceGetMatchingService.restype  = ctypes.c_uint32
IOKit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, _CFDictRef]
IOKit.IOHIDDeviceCreate.restype  = _IOHIDRef
IOKit.IOHIDDeviceCreate.argtypes = [_CFAllocRef, ctypes.c_uint32]
IOKit.IOHIDDeviceOpen.restype    = _IOReturn
IOKit.IOHIDDeviceOpen.argtypes   = [_IOHIDRef, ctypes.c_uint32]
IOKit.IOHIDDeviceClose.restype   = _IOReturn
IOKit.IOHIDDeviceClose.argtypes  = [_IOHIDRef, ctypes.c_uint32]
IOKit.IOHIDDeviceScheduleWithRunLoop.restype  = None
IOKit.IOHIDDeviceScheduleWithRunLoop.argtypes = [_IOHIDRef, ctypes.c_void_p, ctypes.c_void_p]
IOKit.IOHIDDeviceSetReport.restype  = _IOReturn
IOKit.IOHIDDeviceSetReport.argtypes = [_IOHIDRef, ctypes.c_uint32, _CFIndex,
                                        ctypes.c_char_p, _CFIndex]
IOKit.IOHIDDeviceRegisterInputReportCallback.restype  = None
IOKit.IOHIDDeviceRegisterInputReportCallback.argtypes = [
    _IOHIDRef, ctypes.c_char_p, _CFIndex, ctypes.c_void_p, ctypes.c_void_p]
IOKit.IOHIDDeviceRegisterInputValueCallback.restype  = None
IOKit.IOHIDDeviceRegisterInputValueCallback.argtypes = [_IOHIDRef, ctypes.c_void_p, ctypes.c_void_p]

# Correct 7-parameter callback type for IOHIDDeviceRegisterInputReportCallback
IO_HID_REPORT_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,               # context
    ctypes.c_int32,                # IOReturn result
    ctypes.c_void_p,               # sender (IOHIDDeviceRef)
    ctypes.c_uint32,               # IOHIDReportType
    ctypes.c_uint32,               # reportID
    ctypes.POINTER(ctypes.c_uint8),# report buffer
    ctypes.c_ssize_t,              # reportLength
)

IO_HID_VALUE_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)


def build_frame(cmd, data=None):
    """Build DLE/STX framed 64-byte HID packet."""
    payload = [cmd] + (list(data) if data else [])
    length = len(payload)
    chk = length
    for b in payload:
        chk ^= b
    frame = bytes([0x10, 0x02, 0x00, 0x01, length] + payload + [0x10, 0x03, chk])
    return frame.ljust(REPORT_SIZE, b'\x00')


def main():
    print("=== DSP-408 IOKit HID Terminal Test ===")
    print("(run from Terminal.app for Input Monitoring access)\n")

    # Find device via hidapi path → entry ID
    try:
        import hid
        devs = hid.enumerate(0x0483, 0x5750)
        if not devs:
            print("DSP-408 NOT FOUND (VID=0x0483 PID=0x5750)")
            sys.exit(1)
        path = devs[0]['path']
        print(f"Device path: {path!r}")
    except ImportError:
        print("hidapi not available")
        sys.exit(1)

    entry_id = int(path.split(b':')[1])
    matching = IOKit.IORegistryEntryIDMatching(ctypes.c_uint64(entry_id))
    service_t = IOKit.IOServiceGetMatchingService(0, matching)
    if not service_t:
        print(f"IOServiceGetMatchingService failed for entry ID {entry_id}")
        sys.exit(1)
    print(f"io_service_t: {service_t:#010x}")

    dev = IOKit.IOHIDDeviceCreate(kCFAllocatorDefault, ctypes.c_uint32(service_t))
    if not dev:
        print("IOHIDDeviceCreate failed")
        sys.exit(1)

    ret = IOKit.IOHIDDeviceOpen(dev, kIOHIDOptionsTypeNone)
    print(f"IOHIDDeviceOpen: {ret:#010x} ({'OK' if ret == 0 else 'FAILED'})")
    if ret != 0:
        sys.exit(1)

    # Schedule on main thread's run loop
    rl = CF.CFRunLoopGetCurrent()
    IOKit.IOHIDDeviceScheduleWithRunLoop(dev, rl, kCFRunLoopDefaultMode)
    print("Scheduled on run loop")

    # Register REPORT callback
    report_buf = (ctypes.c_uint8 * REPORT_SIZE)()
    report_count = [0]
    def report_cb(ctx, result, sender, rtype, rid, rptr, rlen):
        report_count[0] += 1
        n = min(rlen, REPORT_SIZE)
        data = bytes(report_buf[:n])
        print(f"\n  *** REPORT CALLBACK #{report_count[0]}: len={rlen} data={data[:16].hex(' ')}")
    rcb = IO_HID_REPORT_CALLBACK(report_cb)
    IOKit.IOHIDDeviceRegisterInputReportCallback(
        dev, ctypes.cast(report_buf, ctypes.c_char_p), REPORT_SIZE, rcb, None)
    print("Report callback registered (7-param)")

    # Register VALUE callback
    value_count = [0]
    def value_cb(ctx, result, sender, value_ref):
        value_count[0] += 1
        print(f"\n  *** VALUE CALLBACK #{value_count[0]}: result={result:#010x}")
    vcb = IO_HID_VALUE_CALLBACK(value_cb)
    IOKit.IOHIDDeviceRegisterInputValueCallback(dev, vcb, None)
    print("Value callback registered\n")

    # Send test commands
    tests = [
        ("OP_INIT  (0x10)", build_frame(0x10, [0x10])),
        ("OP_FW    (0x13)", build_frame(0x13, [0x13])),
        ("OP_POLL  (0x40)", build_frame(0x40, [0x40])),
    ]

    for label, frame in tests:
        print(f"→ Sending {label}...")
        ret = IOKit.IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, frame, REPORT_SIZE)
        if ret != 0:
            print(f"  SetReport FAILED: {ret:#010x}")
            continue
        print(f"  SetReport OK, pumping run loop for 1s...")

        deadline = time.monotonic() + 1.0
        iters = 0
        while time.monotonic() < deadline:
            r = CF.CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, True)
            iters += 1
            # 4 = kCFRunLoopRunHandledSource
            if r == 4 and (report_count[0] > 0 or value_count[0] > 0):
                print(f"  → Handled source! ({iters} iters)")
                break

        print(f"  Reports: {report_count[0]}, Values: {value_count[0]}, Iters: {iters}")
        report_count[0] = 0
        value_count[0] = 0
        print()

    IOKit.IOHIDDeviceClose(dev, kIOHIDOptionsTypeNone)
    print("Device closed.")


if __name__ == '__main__':
    main()
