#!/usr/bin/env python3
"""
List all HID devices. Run before and after plugging in the DSP-408
to identify its VID/PID.
"""
import hid

devices = hid.enumerate()
if not devices:
    print("No HID devices found.")
else:
    for d in sorted(devices, key=lambda x: (x['vendor_id'], x['product_id'])):
        print(
            f"VID=0x{d['vendor_id']:04x}  PID=0x{d['product_id']:04x}"
            f"  Usage={d['usage_page']:04x}/{d['usage']:04x}"
            f"  '{d['manufacturer_string']}' / '{d['product_string']}'"
            f"  S/N='{d['serial_number']}'"
            f"  path={d['path']}"
        )
