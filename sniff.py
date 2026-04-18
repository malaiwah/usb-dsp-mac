#!/usr/bin/env python3
"""
Open a HID device by VID/PID and dump every incoming report as hex.
Usage: python sniff.py <VID_hex> <PID_hex>
e.g.:  python sniff.py 0483 5750
"""
import sys
import hid
import time

def hexdump(data):
    hex_str = ' '.join(f'{b:02x}' for b in data)
    asc_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
    return f"{hex_str:<50s}  |{asc_str}|"

def main():
    if len(sys.argv) < 3:
        print("Usage: sniff.py <VID_hex> <PID_hex>")
        sys.exit(1)

    vid = int(sys.argv[1], 16)
    pid = int(sys.argv[2], 16)

    print(f"Opening HID device VID=0x{vid:04x} PID=0x{pid:04x} ...")
    dev = hid.device()
    dev.open(vid, pid)
    dev.set_nonblocking(True)

    mfg = dev.get_manufacturer_string()
    prod = dev.get_product_string()
    print(f"  Manufacturer : {mfg}")
    print(f"  Product      : {prod}")
    print(f"  Listening for reports (Ctrl+C to stop)...\n")

    pkt = 0
    try:
        while True:
            data = dev.read(65)
            if data:
                pkt += 1
                ts = time.time()
                print(f"[{ts:.3f}] #{pkt:04d} IN  len={len(data):2d}  {hexdump(data)}")
            else:
                time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        dev.close()

if __name__ == '__main__':
    main()
