#!/usr/bin/env python3
"""
Watch for a new HID device being plugged in.
Run this before plugging in the DSP-408.
Prints the new device's VID/PID so you can pass it to sniff.py / probe.py.
"""
import hid
import time

def get_devices():
    return {d['path']: d for d in hid.enumerate()}

print("Watching for new HID devices... (plug in the DSP-408 now)")
known = get_devices()

try:
    while True:
        time.sleep(0.5)
        current = get_devices()
        new_paths = set(current) - set(known)
        for path in new_paths:
            d = current[path]
            vid, pid = d['vendor_id'], d['product_id']
            print(f"\n*** NEW DEVICE DETECTED ***")
            print(f"  VID : 0x{vid:04x}")
            print(f"  PID : 0x{pid:04x}")
            print(f"  Manufacturer : {d['manufacturer_string']}")
            print(f"  Product      : {d['product_string']}")
            print(f"  Usage page   : 0x{d['usage_page']:04x}")
            print(f"  Usage        : 0x{d['usage']:04x}")
            print(f"  Path         : {path}")
            print(f"\nNext steps:")
            print(f"  .venv/bin/python sniff.py {vid:04x} {pid:04x}")
            print(f"  .venv/bin/python probe.py {vid:04x} {pid:04x}")
        removed = set(known) - set(current)
        for path in removed:
            d = known[path]
            print(f"\n--- Device removed: 0x{d['vendor_id']:04x}:0x{d['product_id']:04x} {d['product_string']}")
        known = current
except KeyboardInterrupt:
    print("\nStopped.")
