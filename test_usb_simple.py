#!/usr/bin/env python3
import hid
import time

VID = 0x0483
PID = 0x5750

print("Opening device...")
dev = hid.device()
try:
    dev.open(VID, PID)
    print("Device opened successfully!")
    print(f"Manufacturer: {dev.get_manufacturer_string()}")
    print(f"Product: {dev.get_product_string()}")
    
    # Try simple write without initialization
    print("\nSending test write...")
    buf = bytes([0x10, 0x02, 0x00, 0x01, 0x01, 0x10, 0x10, 0x03, 0x11])
    buf = buf.ljust(64, b'\x00')
    dev.write(buf)
    print("Write succeeded")
    
    # Try to read
    print("Reading response...")
    dev.set_nonblocking(1)
    for i in range(10):
        data = dev.read(64)
        if data:
            print(f"Response: {bytes(data).hex()}")
            break
        time.sleep(0.1)
    else:
        print("No response received")
    
    dev.close()
except Exception as e:
    print(f"Error: {e}")
