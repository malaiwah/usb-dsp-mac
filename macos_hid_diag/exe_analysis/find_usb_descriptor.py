#!/usr/bin/env python3
"""Locate the USB device descriptor inside DSP-408-Firmware.bin.

The kernel reports bcdDevice = 0x0200 for VID 0x0483 / PID 0x5750. The
firmware therefore contains a USB device descriptor matching:

  0x12 0x01 [bcdUSB:2] [class:1] [sub:1] [proto:1] [maxpkt:1]
  0x83 0x04 0x50 0x57 [bcdDevice:2] [iMan:1] [iProd:1] [iSer:1] [nCfg:1]

We anchor on the VID/PID/bcdDevice fingerprint `83 04 50 57 00 02` and walk
backward 8 bytes to recover the full 18-byte descriptor.

Also scans for other version-coded fields the device might expose to a
firmware-version HID query (e.g. "MYDW-AV1.06", iProduct/iManufacturer
strings). The byte offsets here are exactly the ones to patch if we want to
spoof a different bcdDevice.
"""
from pathlib import Path

FW = Path("/Users/mbelleau/Code/usb_dsp_mac/downloads/DSP-408-Firmware.bin")

data = FW.read_bytes()


def find_all(needle: bytes) -> list[int]:
    out, pos = [], 0
    while True:
        i = data.find(needle, pos)
        if i < 0: return out
        out.append(i); pos = i + 1


# 1. Anchor on the unique VID/PID/bcdDevice signature
anchor = bytes.fromhex("83 04 50 57 00 02".replace(" ", ""))
hits = find_all(anchor)
print(f"VID/PID/bcdDevice anchor `{anchor.hex(' ')}` hits: {hits}")
print()

for hit in hits:
    desc_start = hit - 8
    if desc_start < 0:
        continue
    desc = data[desc_start: desc_start + 18]
    if desc[0] != 0x12 or desc[1] != 0x01:
        # Not a USB device descriptor — false positive
        print(f"  @ 0x{desc_start:x}: not a device descriptor (bLength=0x{desc[0]:02x},"
              f" bDescriptorType=0x{desc[1]:02x})")
        continue
    print(f"USB Device Descriptor @ 0x{desc_start:x} (= {desc_start}):")
    print(f"  raw: {desc.hex(' ')}")
    print(f"  bLength            = {desc[0]}")
    print(f"  bDescriptorType    = {desc[1]} (=1 → DEVICE)")
    print(f"  bcdUSB             = 0x{int.from_bytes(desc[2:4],'little'):04x}")
    print(f"  bDeviceClass       = 0x{desc[4]:02x}")
    print(f"  bDeviceSubClass    = 0x{desc[5]:02x}")
    print(f"  bDeviceProtocol    = 0x{desc[6]:02x}")
    print(f"  bMaxPacketSize0    = {desc[7]}")
    print(f"  idVendor           = 0x{int.from_bytes(desc[8:10],'little'):04x}")
    print(f"  idProduct          = 0x{int.from_bytes(desc[10:12],'little'):04x}")
    print(f"  bcdDevice          = 0x{int.from_bytes(desc[12:14],'little'):04x}"
          f"  ← patchable @ file offset 0x{desc_start + 12:x}/0x{desc_start + 13:x}")
    print(f"  iManufacturer      = {desc[14]}")
    print(f"  iProduct           = {desc[15]}")
    print(f"  iSerialNumber      = {desc[16]}")
    print(f"  bNumConfigurations = {desc[17]}")
    print()

# 2. Look for any other 0x0200 short that could be a version mirror
shorts = find_all(b"\x00\x02")
print(f"All `00 02` little-endian uint16=0x0200 occurrences: {len(shorts)}")
print(f"  first 10: {[hex(s) for s in shorts[:10]]}")
print()

# 3. Locate the firmware version string MYDW-AV1.XX
ver_off = data.find(b"MYDW-AV")
if ver_off >= 0:
    s = data[ver_off: ver_off + 12]
    print(f"MYDW-AV version string @ 0x{ver_off:x}: {s!r}")
    print(f"  Patchable bytes 0x{ver_off:x}..0x{ver_off + 11:x}")

# 4. iProduct/iManufacturer strings (UTF-16-LE in USB string descriptors)
target = "Audio_Equipment".encode("utf-16-le")
hits = find_all(target)
print(f"\n'Audio_Equipment' (UTF-16-LE) hits: {[hex(h) for h in hits]}")
for hit in hits:
    # USB string descriptor: bLength, bDescriptorType=3, then UTF-16-LE string
    if hit >= 2 and data[hit - 1] == 0x03:
        bLen = data[hit - 2]
        print(f"  @ 0x{hit:x}: looks like USB string descriptor, "
              f"bLength={bLen}, bDescriptorType=3")
