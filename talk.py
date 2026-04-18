#!/usr/bin/env python3
"""
DSP-408 USB communicator via hidapi (macOS IOHIDManager).
Usage: .venv/bin/python talk.py

Requires Input Monitoring permission for the calling app.
"""
import hid, time, sys

VID, PID = 0x0483, 0x5750
PKT = 64

def hexdump(data):
    h = ' '.join(f'{b:02x}' for b in data)
    a = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
    return f'{h}  |{a}|'

def send_recv(dev, payload, label='', timeout=0.3):
    buf = bytes([0x00]) + bytes(payload)[:PKT] + b'\x00' * max(0, PKT - len(payload))
    print(f'  >>> [{label}] {" ".join(f"{b:02x}" for b in buf[1:9])}...')
    dev.write(buf)
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = dev.read(PKT)
        if data:
            print(f'  <<< [{label}] {hexdump(bytes(data))}')
            return bytes(data)
        time.sleep(0.01)
    print(f'  <<< [{label}] (no response)')
    return None

def main():
    print(f'Enumerating HID devices for VID={VID:#06x} PID={PID:#06x}...')
    devs = hid.enumerate(VID, PID)
    if not devs:
        print('Device not found. Is it plugged in and powered?')
        sys.exit(1)

    d = devs[0]
    print(f'Found: {d["product_string"]!r}  S/N={d["serial_number"]!r}  path={d["path"]}')

    dev = hid.device()
    dev.open_path(d['path'])
    dev.set_nonblocking(True)
    print('Opened OK.\n')

    print('─── Phase 1: listen for spontaneous output (1.5s) ───')
    deadline = time.time() + 1.5
    got = False
    while time.time() < deadline:
        data = dev.read(PKT)
        if data:
            print(f'  SPONTANEOUS: {hexdump(bytes(data))}')
            got = True
        time.sleep(0.01)
    if not got:
        print('  (nothing)')

    print('\n─── Phase 2: single-byte command probes ───')
    for cmd in [0x01, 0x02, 0x03, 0x10, 0x11, 0x12, 0x13, 0x20, 0x21, 0xA0, 0xB0, 0xFF]:
        send_recv(dev, bytes([cmd]), f'cmd_{cmd:02x}')

    print('\n─── Phase 3: t.racks DLE/STX framing ───')
    tracks = [
        ([0x10,0x02,0x00,0x01,0x01,0x01,0x10,0x03,0x11], 'handshake'),
        ([0x10,0x02,0x00,0x01,0x01,0x13,0x10,0x03,0x12], 'devinfo'),
        ([0x10,0x02,0x00,0x01,0x01,0x12,0x10,0x03,0x13], 'status'),
        ([0x10,0x02,0x00,0x01,0x01,0x40,0x10,0x03,0x41], 'keepalive'),
        ([0x10,0x02,0x00,0x01,0x01,0x2c,0x10,0x03,0x2d], 'preset_count'),
    ]
    for payload, label in tracks:
        send_recv(dev, payload, f't_{label}')

    print('\n─── Phase 4: listen again (2s) ───')
    deadline = time.time() + 2.0
    got = False
    while time.time() < deadline:
        data = dev.read(PKT)
        if data:
            print(f'  DELAYED: {hexdump(bytes(data))}')
            got = True
        time.sleep(0.01)
    if not got:
        print('  (nothing)')

    dev.close()
    print('\nDone.')

if __name__ == '__main__':
    main()
