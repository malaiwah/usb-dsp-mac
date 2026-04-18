#!/usr/bin/env python3
"""Enable LIBUSB_DEBUG before importing PyUSB to capture libusb debug output."""
import os, sys
os.environ['LIBUSB_DEBUG'] = '4'

from usb_transport import USBTransport, build_frame, parse_frame

t = USBTransport()
t.open()
print(f"\nOpened: {t.info()}", file=sys.stderr)

frame = build_frame(0x10)   # OP_INIT, 1-byte payload [0x10]
print(f"\n→ OP_INIT (1-byte payload, correct format)...", file=sys.stderr)
t.send(frame)
raw = t.recv(timeout_ms=2000)
if raw:
    parsed = parse_frame(raw)
    print(f"  ← DATA: parsed={parsed}", file=sys.stderr)
else:
    print(f"  ← TIMEOUT", file=sys.stderr)

# Wait a bit with a pre-submitted read — 5 seconds
print(f"\n→ Waiting 5s for any unsolicited EP_IN data...", file=sys.stderr)
raw = t.recv(timeout_ms=5000)
print(f"  ← {'DATA: ' + raw.hex() if raw else 'TIMEOUT'}", file=sys.stderr)

t.close()
