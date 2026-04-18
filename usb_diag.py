#!/usr/bin/env python3
"""
USB endpoint diagnostic for DSP-408.

Requires root: sudo .venv/bin/python usb_diag.py

Runs after detaching DriverKit:
  1. Dumps full USB configuration/endpoint descriptors
  2. Confirms which pipe index maps to EP_IN 0x82
  3. Tries reads on ALL interrupt IN endpoints (0x81, 0x82, etc.)
  4. Uses IOUSBLib ReadPipeTO directly (bypassing libusb's transfer stack)
  5. Checks /usr/bin/log for IOUSBHost messages during read
"""

from __future__ import annotations
import ctypes
import ctypes.util
import struct
import sys
import threading
import time

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1 as _lb1
except ImportError:
    raise ImportError("pip install pyusb")

VID = 0x0483
PID = 0x5750
INTERFACE = 0
REPORT_SIZE = 64

# ── Frame helpers ─────────────────────────────────────────────────────────────
def build_frame(cmd: int, data: list = None) -> bytes:
    payload = [cmd] + (list(data) if data else [])
    n = len(payload)
    chk = n
    for b in payload:
        chk ^= b
    return (bytes([0x10, 0x02, 0x00, 0x01, n]) + bytes(payload) +
            bytes([0x10, 0x03, chk])).ljust(REPORT_SIZE, b'\x00')


OP_INIT  = build_frame(0x10, [0x10])
OP_POLL  = build_frame(0x40, [0x40])


# ── Step 1: Find + detach + claim ─────────────────────────────────────────────
print("=" * 60)
print("1. Finding device via PyUSB")
print("=" * 60)

dev = usb.core.find(idVendor=VID, idProduct=PID)
if dev is None:
    print("ERROR: Device not found")
    sys.exit(1)

print(f"Found: bus={dev.bus} addr={dev.address} speed={dev.speed}")

# Detach
try:
    active = dev.is_kernel_driver_active(INTERFACE)
    print(f"Kernel driver active: {active}")
    if active:
        dev.detach_kernel_driver(INTERFACE)
        print("Detached DriverKit driver")
except Exception as e:
    print(f"Detach: {e}")

# Set configuration
try:
    dev.set_configuration()
    print("set_configuration() OK")
except Exception as e:
    print(f"set_configuration: {e} (may be normal)")

# Claim interface
usb.util.claim_interface(dev, INTERFACE)
print(f"Claimed interface {INTERFACE}")


# ── Step 2: Dump ALL endpoint descriptors ─────────────────────────────────────
print()
print("=" * 60)
print("2. USB descriptor dump")
print("=" * 60)

cfg = dev.get_active_configuration()
print(f"Active configuration: bConfigurationValue={cfg.bConfigurationValue}")

for intf in cfg:
    print(f"\n  Interface: bInterfaceNumber={intf.bInterfaceNumber} "
          f"bAlternateSetting={intf.bAlternateSetting} "
          f"bInterfaceClass={intf.bInterfaceClass:#04x} "
          f"bInterfaceSubClass={intf.bInterfaceSubClass:#04x} "
          f"bInterfaceProtocol={intf.bInterfaceProtocol:#04x}")
    for ep in intf:
        direction = "IN" if (ep.bEndpointAddress & 0x80) else "OUT"
        ep_type = {
            0: "Control",
            1: "Isochronous",
            2: "Bulk",
            3: "Interrupt",
        }.get(ep.bmAttributes & 0x03, "Unknown")
        print(f"    Endpoint: addr={ep.bEndpointAddress:#04x} ({direction}) "
              f"type={ep_type} maxPkt={ep.wMaxPacketSize} interval={ep.bInterval}")

# Find all IN interrupt endpoints
in_eps = []
for intf in cfg:
    if intf.bInterfaceNumber == INTERFACE:
        for ep in intf:
            if (ep.bEndpointAddress & 0x80) and ((ep.bmAttributes & 0x03) == 3):
                in_eps.append(ep.bEndpointAddress)

out_eps = []
for intf in cfg:
    if intf.bInterfaceNumber == INTERFACE:
        for ep in intf:
            if not (ep.bEndpointAddress & 0x80) and ((ep.bmAttributes & 0x03) == 3):
                out_eps.append(ep.bEndpointAddress)

print(f"\nInterrupt IN endpoints on interface {INTERFACE}: {[hex(a) for a in in_eps]}")
print(f"Interrupt OUT endpoints on interface {INTERFACE}: {[hex(a) for a in out_eps]}")


# ── Step 3: IOUSBLib direct read via ctypes ────────────────────────────────────
print()
print("=" * 60)
print("3. IOUSBLib direct ReadPipeTO via ctypes")
print("=" * 60)

# Load IOKit
IOKit = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
CF    = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

# IOKit functions we need
IOKit.IOServiceMatching.restype  = ctypes.c_void_p
IOKit.IOServiceMatching.argtypes = [ctypes.c_char_p]
IOKit.IOServiceGetMatchingServices.restype  = ctypes.c_int32
IOKit.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_uint32)]
IOKit.IOIteratorNext.restype  = ctypes.c_uint32
IOKit.IOIteratorNext.argtypes = [ctypes.c_uint32]
IOKit.IOObjectRelease.restype  = ctypes.c_int32
IOKit.IOObjectRelease.argtypes = [ctypes.c_uint32]
IOKit.IORegistryEntryCreateCFProperties.restype  = ctypes.c_int32
IOKit.IORegistryEntryCreateCFProperties.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
                                                      ctypes.c_void_p, ctypes.c_uint32]
IOKit.IOCreatePlugInInterfaceForService.restype  = ctypes.c_int32
IOKit.IOCreatePlugInInterfaceForService.argtypes = [ctypes.c_uint32,
                                                      ctypes.c_void_p, ctypes.c_void_p,
                                                      ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
                                                      ctypes.POINTER(ctypes.c_int32)]
IOKit.IORegistryEntryIDMatching.restype  = ctypes.c_void_p
IOKit.IORegistryEntryIDMatching.argtypes = [ctypes.c_uint64]
IOKit.IOServiceGetMatchingService.restype  = ctypes.c_uint32
IOKit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]

# Get the IOUSBInterface via libusb's existing handle
# libusb already has the interface open; we can't easily get the IOUSBInterfaceInterface
# without going through the plugin ourselves.
# Instead, let's just use libusb directly but with lower-level read approach.

# ── Step 4: PyUSB read tests ───────────────────────────────────────────────────
print()
print("=" * 60)
print("4. PyUSB interrupt read tests")
print("=" * 60)

EP_OUT_ADDR = out_eps[0] if out_eps else 0x01
EP_IN_ADDR  = in_eps[0]  if in_eps  else 0x82

print(f"Using EP_OUT={EP_OUT_ADDR:#04x} EP_IN={EP_IN_ADDR:#04x}")

# 4a. Read BEFORE write with long timeout (4 seconds)
print("\n[4a] Pre-read (4s timeout, before any write)...")
try:
    data = dev.read(EP_IN_ADDR, REPORT_SIZE, timeout=4000)
    print(f"  GOT DATA: {bytes(data)[:16].hex(' ')}...")
except usb.core.USBTimeoutError:
    print("  Timeout (expected — device won't send unprompted data)")
except Exception as e:
    print(f"  Error: {e}")

# 4b. Write OP_INIT then read immediately
print("\n[4b] Write OP_INIT (0x10), then read with 2s timeout...")
try:
    n = dev.write(EP_OUT_ADDR, OP_INIT, timeout=1000)
    print(f"  Write: n={n} bytes")
except Exception as e:
    print(f"  Write failed: {e}")
    n = 0

if n:
    try:
        data = dev.read(EP_IN_ADDR, REPORT_SIZE, timeout=2000)
        print(f"  GOT RESPONSE: {bytes(data)[:16].hex(' ')}...")
    except usb.core.USBTimeoutError:
        print("  Timeout — no response")
    except Exception as e:
        print(f"  Read error: {e}")

# 4c. Write OP_POLL then read
print("\n[4c] Write OP_POLL (0x40), then read with 2s timeout...")
try:
    n = dev.write(EP_OUT_ADDR, OP_POLL, timeout=1000)
    print(f"  Write: n={n} bytes")
except Exception as e:
    print(f"  Write failed: {e}")
    n = 0

if n:
    try:
        data = dev.read(EP_IN_ADDR, REPORT_SIZE, timeout=2000)
        print(f"  GOT RESPONSE: {bytes(data)[:16].hex(' ')}...")
    except usb.core.USBTimeoutError:
        print("  Timeout — no response")
    except Exception as e:
        print(f"  Read error: {e}")

# 4d. Try ALL interrupt IN endpoints found
if len(in_eps) > 1 or (in_eps and in_eps[0] != 0x82):
    print(f"\n[4d] Trying all interrupt IN endpoints: {[hex(a) for a in in_eps]}")
    for ep_addr in in_eps:
        # Send init first
        try:
            dev.write(EP_OUT_ADDR, OP_INIT, timeout=1000)
        except Exception:
            pass
        print(f"  Trying EP {ep_addr:#04x}...")
        try:
            data = dev.read(ep_addr, REPORT_SIZE, timeout=1500)
            print(f"    GOT DATA: {bytes(data)[:16].hex(' ')}...")
        except usb.core.USBTimeoutError:
            print(f"    Timeout")
        except Exception as e:
            print(f"    Error: {e}")

# 4e. Clear halt on EP_IN and retry
print("\n[4e] Clear halt on EP_IN, then write+read...")
try:
    dev.clear_halt(EP_IN_ADDR)
    print(f"  clear_halt({EP_IN_ADDR:#04x}) OK")
except Exception as e:
    print(f"  clear_halt: {e}")

for label, frame in [("OP_INIT", OP_INIT), ("OP_POLL", OP_POLL)]:
    try:
        dev.write(EP_OUT_ADDR, frame, timeout=1000)
    except Exception as e:
        print(f"  Write {label} failed: {e}")
        continue
    try:
        data = dev.read(EP_IN_ADDR, REPORT_SIZE, timeout=2000)
        print(f"  {label} response: {bytes(data)[:16].hex(' ')}...")
    except usb.core.USBTimeoutError:
        print(f"  {label} → Timeout")
    except Exception as e:
        print(f"  {label} read error: {e}")

# 4f. Threaded read-before-write
print("\n[4f] Threaded: reader starts BEFORE writer, 3s window...")

result = [None]
err = [None]

def reader_thread():
    try:
        result[0] = bytes(dev.read(EP_IN_ADDR, REPORT_SIZE, timeout=3000))
    except usb.core.USBTimeoutError:
        err[0] = "TIMEOUT"
    except Exception as e:
        err[0] = str(e)

t = threading.Thread(target=reader_thread, daemon=True)
t.start()
time.sleep(0.15)  # let reader submit its transfer

for label, frame in [("OP_INIT", OP_INIT), ("OP_POLL", OP_POLL)]:
    try:
        n = dev.write(EP_OUT_ADDR, frame, timeout=1000)
        print(f"  Wrote {label} ({n}B)")
    except Exception as e:
        print(f"  Write {label} failed: {e}")

t.join(timeout=4.0)
if result[0]:
    print(f"  READER GOT: {result[0][:16].hex(' ')}...")
elif err[0]:
    print(f"  Reader result: {err[0]}")
else:
    print("  Reader still running (join timed out)")


# ── Step 5: Check IORegistry counters before/after ───────────────────────────
print()
print("=" * 60)
print("5. Checking device state")
print("=" * 60)

import subprocess

def get_ioreg_counters():
    try:
        r = subprocess.run(['ioreg', '-n', 'DSP-408', '-d', '5', '-w', '0'],
                           capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
        for line in lines:
            if any(k in line for k in ('InputReportCount', 'SetReportCount',
                                        'OutputReportCount', 'HIDDKStart')):
                print(f"  {line.strip()}")
    except Exception as e:
        print(f"  ioreg error: {e}")

print("IORegistry counters:")
get_ioreg_counters()


# ── Cleanup ───────────────────────────────────────────────────────────────────
print()
try:
    usb.util.release_interface(dev, INTERFACE)
    dev.attach_kernel_driver(INTERFACE)
    print("Released interface and reattached DriverKit driver")
except Exception as e:
    print(f"Cleanup: {e}")
finally:
    usb.util.dispose_resources(dev)

print("\nDone.")
