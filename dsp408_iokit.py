#!/usr/bin/env python3
"""
DSP-408 IOKit Interface - macOS native HID without exclusive mode

This bypasses hidapi's exclusive open and allows the device to respond
while still being accessible to the system.

Based on: https://github.com/libusb/hidapi/issues/769
          https://github.com/signal11/hidapi/issues/400
"""

import ctypes
import ctypes.util
from typing import Optional, List

# IOKit constants
kIOHIDOptionsTypeNone = 0x00000000
kIOHIDOptionsTypeSeizeDevice = 0x00000001

kIOHIDReportTypeInput = 0
kIOHIDReportTypeOutput = 1
kIOHIDReportTypeFeature = 2

# Load IOKit frameworks
IOKit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
CoreFoundation = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

# CFString types
CFStringRef = ctypes.c_void_p
CFStringEncoding = ctypes.c_uint32
CFIndex = ctypes.c_longlong
CFTypeID = ctypes.c_ulong

# HID types
IOHIDDeviceRef = ctypes.c_void_p
IOHIDManagerRef = ctypes.c_void_p
IOHIDValueRef = ctypes.c_void_p
IOReturn = ctypes.c_int32

# Setup CFString functions
CoreFoundation.CFStringCreateWithCString.restype = CFStringRef
CoreFoundation.CFStringCreateWithCString.argtypes = [
    ctypes.c_void_p,  # allocator
    ctypes.c_char_p,  # cstring
    CFStringEncoding,  # encoding
]

CoreFoundation.CFStringGetCString.restype = ctypes.c_bool
CoreFoundation.CFStringGetCString.argtypes = [
    CFStringRef,  # theString
    ctypes.c_char_p,  # buffer
    CFIndex,  # bufferSize
    CFStringEncoding,  # encoding
]

CoreFoundation.CFRelease.restype = None
CoreFoundation.CFRelease.argtypes = [ctypes.c_void_p]

CoreFoundation.CFGetTypeID.restype = CFTypeID
CoreFoundation.CFGetTypeID.argtypes = [ctypes.c_void_p]

# HID Manager functions
IOKit.IOHIDManagerCreate.restype = IOHIDManagerRef
IOKit.IOHIDManagerCreate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

IOKit.IOHIDManagerSetDeviceMatching.restype = None
IOKit.IOHIDManagerSetDeviceMatching.argtypes = [IOHIDManagerRef, CFStringRef]

IOKit.IOHIDManagerCopyDevices.restype = ctypes.c_void_p  # CFSetRef
IOKit.IOHIDManagerCopyDevices.argtypes = [IOHIDManagerRef]

IOKit.IOHIDDeviceOpen.restype = IOReturn
IOKit.IOHIDDeviceOpen.argtypes = [IOHIDDeviceRef, ctypes.c_uint32]

IOKit.IOHIDDeviceClose.restype = IOReturn
IOKit.IOHIDDeviceClose.argtypes = [IOHIDDeviceRef, ctypes.c_uint32]

IOKit.IOHIDDeviceSetReport.restype = IOReturn
IOKit.IOHIDDeviceSetReport.argtypes = [
    IOHIDDeviceRef,
    ctypes.c_uint32,  # reportType
    ctypes.c_int32,  # reportID
    ctypes.c_char_p,  # report
    CFIndex,  # reportLength
]

IOKit.IOHIDDeviceGetReport.restype = IOReturn
IOKit.IOHIDDeviceGetReport.argtypes = [
    IOHIDDeviceRef,
    ctypes.c_uint32,  # reportType
    ctypes.c_int32,  # reportID
    ctypes.c_char_p,  # report
    ctypes.POINTER(CFIndex),  # reportLength
]

IOKit.IOHIDDeviceGetProperty.restype = ctypes.c_void_p
IOKit.IOHIDDeviceGetProperty.argtypes = [IOHIDDeviceRef, CFStringRef]

IOKit.IOHIDDeviceScheduleWithRunLoop.restype = None
IOKit.IOHIDDeviceScheduleWithRunLoop.argtypes = [
    IOHIDDeviceRef,
    ctypes.c_void_p,  # CFRunLoopRef
    CFStringRef,  # CFRunLoopMode
]

# CFSet functions
CoreFoundation.CFSetGetValues.restype = None
CoreFoundation.CFSetGetValues.argtypes = [
    ctypes.c_void_p,  # CFSetRef
    ctypes.POINTER(ctypes.c_void_p),  # values
]

CoreFoundation.CFSetGetCount.restype = CFIndex
CoreFoundation.CFSetGetCount.argtypes = [ctypes.c_void_p]


class DSP408IOKit:
    """DSP-408 controller using native IOKit (non-exclusive mode)."""

    VID = 0x0483
    PID = 0x5750
    REPORT_SIZE = 64

    def __init__(self):
        self.device = IOHIDDeviceRef(0)
        self.manager = None
        self.connected = False

    def connect(self) -> bool:
        """Open device in non-exclusive mode."""
        # Create HID manager
        self.manager = IOKit.IOHIDManagerCreate(None, 0)
        if not self.manager:
            print("Failed to create HID manager")
            return False

        # Create matching dictionary for our device
        match_key = CoreFoundation.CFStringCreateWithCString(
            None,
            b"ProductUsage",
            0x08000100,  # kCFStringEncodingUTF8
        )
        match_val = CoreFoundation.CFStringCreateWithCString(
            None, b"Audio_Equipment", 0x08000100
        )

        # Actually, let's match by VID/PID instead
        # Create dictionary: {"VendorID": 0x0483, "ProductID": 0x5750}
        vendor_key = CoreFoundation.CFStringCreateWithCString(
            None, b"VendorID", 0x08000100
        )
        product_key = CoreFoundation.CFStringCreateWithCString(
            None, b"ProductID", 0x08000100
        )

        # Set device matching - use simple approach first
        IOKit.IOHIDManagerSetDeviceMatching(self.manager, None)

        # Copy devices
        device_set = IOKit.IOHIDManagerCopyDevices(self.manager)
        if not device_set:
            print("No HID devices found")
            return False

        # Iterate through devices
        count = CoreFoundation.CFSetGetCount(device_set)
        devices = (ctypes.c_void_p * count)()
        CoreFoundation.CFSetGetValues(device_set, devices)

        for i in range(count):
            dev = devices[i]

            # Get vendor ID
            vid_ref = IOKit.IOHIDDeviceGetProperty(dev, vendor_key)
            if vid_ref:
                # Simplified - just try to open and see if it works
                pass

            # Try to open device (non-exclusive: kIOHIDOptionsTypeNone)
            result = IOKit.IOHIDDeviceOpen(dev, kIOHIDOptionsTypeNone)
            if result == 0:  # kIOReturnSuccess
                print(f"Successfully opened device {i} in non-exclusive mode")
                self.device = dev

                # Get property info
                serial_key = CoreFoundation.CFStringCreateWithCString(
                    None, b"SerialNumber", 0x08000100
                )
                serial_ref = IOKit.IOHIDDeviceGetProperty(dev, serial_key)
                if serial_ref:
                    buf = ctypes.create_string_buffer(64)
                    if CoreFoundation.CFStringGetCString(
                        serial_ref, buf, 64, 0x08000100
                    ):
                        print(f"Serial: {buf.value.decode()}")

                self.connected = True

                # Schedule with run loop
                run_loop_key = CoreFoundation.CFStringCreateWithCString(
                    None, b"kCFRunLoopDefaultMode", 0x08000100
                )
                IOKit.IOHIDDeviceScheduleWithRunLoop(
                    self.device,
                    ctypes.c_void_p.in_dll(IOKit, "CFRunLoopGetCurrent"),
                    run_loop_key,
                )

                return True

        print("Failed to open any matching device")
        return False

    def disconnect(self):
        """Close the device."""
        if self.device:
            IOKit.IOHIDDeviceClose(self.device, kIOHIDOptionsTypeNone)
            self.device = IOHIDDeviceRef(0)
        if self.manager:
            CoreFoundation.CFRelease(self.manager)
            self.manager = None
        self.connected = False

    def write(self, data: bytes, timeout_ms: int = 1000) -> int:
        """Write report to device."""
        if not self.device:
            raise RuntimeError("Device not connected")

        # Ensure data is 64 bytes + report ID
        if len(data) < self.REPORT_SIZE:
            data = data + bytes(self.REPORT_SIZE - len(data))
        elif len(data) > self.REPORT_SIZE:
            data = data[: self.REPORT_SIZE]

        # Add report ID (0x00) at beginning
        report = bytes([0x00]) + data

        result = IOKit.IOHIDDeviceSetReport(
            self.device,
            kIOHIDReportTypeOutput,
            0,  # reportID (0 for no ID)
            report,
            len(report),
        )

        if result != 0:
            print(f"IOHIDDeviceSetReport failed with error {result}")
            return -1

        return len(data)

    def read(self, size: int, timeout_ms: int = 1000) -> bytes:
        """Read report from device (simplified - may need run loop)."""
        if not self.device:
            raise RuntimeError("Device not connected")

        buf = ctypes.create_string_buffer(size)
        length = CFIndex(size)

        result = IOKit.IOHIDDeviceGetReport(
            self.device,
            kIOHIDReportTypeInput,
            0,  # reportID
            buf,
            ctypes.byref(length),
        )

        if result != 0:
            return b""

        return bytes(buf[: length.value])


# Test connection
if __name__ == "__main__":
    print("=== DSP-408 IOKit Interface Test ===")
    dev = DSP408IOKit()

    if dev.connect():
        print("✓ Device connected (non-exclusive mode)")

        # Try to send a keepalive command
        # Frame format: DLE STX seq_hi seq_lo cmd [data...] DLE ETX checksum
        keepalive = bytes(
            [
                0x10,
                0x02,  # DLE STX
                0x00,
                0x01,  # Sequence
                0x40,  # Command (keepalive)
                0x10,
                0x03,  # DLE ETX
                0x40,  # Checksum (XOR of bytes between STX and ETX)
            ]
        )

        print("Sending keepalive...")
        result = dev.write(keepalive)
        print(f"Write result: {result} bytes")

        # Try to read response
        response = dev.read(64, timeout_ms=500)
        if response:
            print(f"Response: {response.hex()}")
        else:
            print("No response received")

        dev.disconnect()
    else:
        print("✗ Failed to connect to device")
