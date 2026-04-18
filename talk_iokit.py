#!/usr/bin/env python3
"""
DSP-408 communicator using macOS IOKit/IOUSBLib directly via ctypes.
This bypasses libusb and talks to Apple's own USB stack.

Run: sudo .venv/bin/python talk_iokit.py
"""
import ctypes, ctypes.util, sys, time, struct

# ── frameworks ────────────────────────────────────────────────────────────────
IOKit = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
CF    = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

# ── CoreFoundation types ──────────────────────────────────────────────────────
CFAllocatorRef    = ctypes.c_void_p
CFTypeRef         = ctypes.c_void_p
CFStringRef       = ctypes.c_void_p
CFDictionaryRef   = ctypes.c_void_p
CFNumberRef       = ctypes.c_void_p
CFNumberType      = ctypes.c_uint32
kCFNumberSInt32Type = 3

CF.CFStringCreateWithCString.restype  = CFStringRef
CF.CFStringCreateWithCString.argtypes = [CFAllocatorRef, ctypes.c_char_p, ctypes.c_uint32]
CF.CFNumberCreate.restype             = CFNumberRef
CF.CFNumberCreate.argtypes            = [CFAllocatorRef, CFNumberType, ctypes.c_void_p]
CF.CFDictionaryCreateMutable.restype  = CFDictionaryRef
CF.CFDictionaryCreateMutable.argtypes = [CFAllocatorRef, ctypes.c_long,
                                         ctypes.c_void_p, ctypes.c_void_p]
CF.CFDictionarySetValue.argtypes      = [CFDictionaryRef, CFTypeRef, CFTypeRef]
CF.CFRelease.argtypes                 = [CFTypeRef]
kCFStringEncodingUTF8 = 0x08000100

# ── IOKit types ───────────────────────────────────────────────────────────────
IOReturn          = ctypes.c_int
io_service_t      = ctypes.c_uint32
io_iterator_t     = ctypes.c_uint32
mach_port_t       = ctypes.c_uint32
kIOReturnSuccess  = 0
kIOMasterPortDefault = 0  # macOS < 12
kIOMainPortDefault   = 0  # macOS >= 12

IOKit.IOServiceGetMatchingServices.restype  = IOReturn
IOKit.IOServiceGetMatchingServices.argtypes = [mach_port_t, CFDictionaryRef,
                                               ctypes.POINTER(io_iterator_t)]
IOKit.IOIteratorNext.restype  = io_service_t
IOKit.IOIteratorNext.argtypes = [io_iterator_t]
IOKit.IOObjectRelease.restype  = IOReturn
IOKit.IOObjectRelease.argtypes = [ctypes.c_uint32]
IOKit.IORegistryEntryGetName.restype  = IOReturn
IOKit.IORegistryEntryGetName.argtypes = [io_service_t, ctypes.c_char_p]

# IOCreatePlugInInterfaceForService
IOKit.IOCreatePlugInInterfaceForService.restype = IOReturn
IOKit.IOCreatePlugInInterfaceForService.argtypes = [
    io_service_t, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int32)
]

# UUIDs needed for USB plugin
kIOUSBDeviceUserClientTypeID_bytes  = bytes.fromhex('2d9786c6950340889999d5837296939c')
kIOCFPlugInInterfaceID_bytes        = bytes.fromhex('c244e858109847b5bb8635363a8d7f6f')
kIOUSBInterfaceUserClientTypeID_bytes = bytes.fromhex('2d9786c695034088999f79bef7f9a7eb')
kIOUSBInterfaceInterfaceID_bytes    = bytes.fromhex('73c97ae8cbbb4079ab9aaa52b5cc7b54')

def make_uuid(b16):
    # struct CFUUIDBytes (16 bytes big-endian)
    class _UUID(ctypes.Structure):
        _fields_ = [('bytes', ctypes.c_uint8 * 16)]
    u = _UUID()
    for i, b in enumerate(b16): u.bytes[i] = b
    return u

def cf_string(s):
    return CF.CFStringCreateWithCString(None, s.encode(), kCFStringEncodingUTF8)

def cf_number(n):
    v = ctypes.c_int32(n)
    return CF.CFNumberCreate(None, kCFNumberSInt32Type, ctypes.byref(v))

def matching_dict(vid, pid):
    d = CF.CFDictionaryCreateMutable(None, 0, None, None)
    k_vid = cf_string('idVendor')
    k_pid = cf_string('idProduct')
    v_vid = cf_number(vid)
    v_pid = cf_number(pid)
    CF.CFDictionarySetValue(d, k_vid, v_vid)
    CF.CFDictionarySetValue(d, k_pid, v_pid)
    return d

# ── find device ───────────────────────────────────────────────────────────────
VID, PID = 0x0483, 0x5750
EP_OUT, EP_IN = 0x01, 0x82
PKT = 64

def hexdump(data):
    h = ' '.join(f'{b:02x}' for b in data)
    a = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
    return f'{h}  |{a}|'

print(f'Searching for VID={VID:#06x} PID={PID:#06x}...')
match = matching_dict(VID, PID)
iterator = io_iterator_t(0)
kr = IOKit.IOServiceGetMatchingServices(kIOMainPortDefault, match, ctypes.byref(iterator))
if kr != kIOReturnSuccess:
    print(f'IOServiceGetMatchingServices failed: {kr:#010x}')
    sys.exit(1)

service = IOKit.IOIteratorNext(iterator)
IOKit.IOObjectRelease(iterator)

if not service:
    print('Device not found.')
    sys.exit(1)

name_buf = ctypes.create_string_buffer(128)
IOKit.IORegistryEntryGetName(service, name_buf)
print(f'Found IOService: {name_buf.value.decode()}')

# ── open device via IOUSBLib plugin ──────────────────────────────────────────
# This uses the Objective-C COM-style plugin interface
# Load IOUSBLib bundle
import subprocess, os
result = subprocess.run(
    [sys.executable, '-c', '''
import objc, sys
from Foundation import NSBundle
from IOKit.usb import *
print("objc USB available")
'''], capture_output=True, text=True)

if 'objc USB available' in result.stdout:
    print('Using PyObjC path...')
else:
    # Fall back to direct IOKit approach
    print('PyObjC USB not available, trying direct IOUSBLib...')
    print()
    print('NOTE: On macOS 12+, accessing USB HID devices from user space requires')
    print('either a DriverKit extension or the Accessibility/Input Monitoring permission.')
    print()
    print('Alternative: check if the device appears in /dev/ as a serial port')
    import glob
    devs = glob.glob('/dev/cu.*') + glob.glob('/dev/tty.*')
    usb_devs = [d for d in devs if 'usb' in d.lower() or 'USB' in d.lower() or 'ACM' in d.lower()]
    if usb_devs:
        print(f'USB serial devices found: {usb_devs}')
    else:
        print('No USB serial devices found either.')

    print()
    print('Trying hidapi one more time with explicit path...')
    import hid
    for d in hid.enumerate():
        print(f'  HID: {d["vendor_id"]:#06x}:{d["product_id"]:#06x} {d["product_string"]!r}')
    try:
        dev = hid.device()
        dev.open(VID, PID)
        print('hidapi open succeeded!')
        dev.set_nonblocking(True)
        for _ in range(20):
            r = dev.read(64)
            if r: print(f'  RX: {bytes(r).hex()}')
            time.sleep(0.05)
        dev.close()
    except Exception as e:
        print(f'hidapi still fails: {e}')

IOKit.IOObjectRelease(service)
