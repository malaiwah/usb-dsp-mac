#!/usr/bin/env python3
"""
Test using hidapi (IOKit HID on macOS) to read from DSP-408.
hidapi uses a dedicated thread + CFRunLoop, unlike our Swift tests which use main runloop.
"""
import sys, time
import hid  # pip install hid

VID, PID = 0x0483, 0x5750
REPORT_SZ = 64

def build_frame(cmd: int) -> bytes:
    n = 1
    chk = n ^ cmd
    raw = bytes([0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk])
    return raw.ljust(REPORT_SZ, b'\x00')

# Enumerate
print("=== hidapi enumerate ===")
devs = hid.enumerate(VID, PID)
if not devs:
    print("Device not found")
    sys.exit(1)
for d in devs:
    print(f"  path={d['path']} usage_page=0x{d['usage_page']:04x} usage=0x{d['usage']:04x}")
    print(f"  manufacturer={d['manufacturer_string']} product={d['product_string']}")

# Open
print("\n=== Opening device ===")
try:
    dev = hid.Device(VID, PID)
    print("Opened OK")
    print(f"  Manufacturer: {dev.manufacturer}")
    print(f"  Product: {dev.product}")
    dev.nonblocking = 1  # Non-blocking reads
except Exception as e:
    print(f"Open failed: {e}")
    sys.exit(1)

# Test commands
cmds = [
    ("OP_INIT 0x10", build_frame(0x10)),
    ("OP_FW   0x13", build_frame(0x13)),
    ("OP_INFO 0x2C", build_frame(0x2C)),
    ("OP_POLL 0x40", build_frame(0x40)),
]

for label, frame in cmds:
    print(f"\n→ {label}")

    # Prepend report ID 0 for hidapi (it prepends the report ID byte)
    frame_with_id = bytes([0x00]) + frame  # report ID 0 prefix

    try:
        n = dev.write(frame_with_id)
        print(f"  write: {n} bytes OK")
    except Exception as e:
        print(f"  write FAILED: {e}")
        # Try without report ID prefix
        try:
            n = dev.write(frame)
            print(f"  write (no ID prefix): {n} bytes OK")
        except Exception as e2:
            print(f"  write (no ID) FAILED: {e2}")
            continue

    # Read with 2s timeout (blocking poll)
    dev.set_nonblocking(0)
    start = time.monotonic()
    try:
        # read() blocks for timeout ms
        data = dev.read(REPORT_SZ, timeout_ms=2000)
        elapsed = time.monotonic() - start
        if data:
            print(f"  ← DATA ({elapsed*1000:.0f}ms): {bytes(data[:16]).hex(' ')}")
        else:
            print(f"  ← TIMEOUT ({elapsed*1000:.0f}ms)")
    except Exception as e:
        print(f"  ← READ ERROR: {e}")
    dev.set_nonblocking(1)

print("\n=== Done ===")
dev.close()
