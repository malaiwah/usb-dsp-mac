#!/usr/bin/env python3
"""Hunt for the 8-byte WMCU trailer signature inside DSP-408.exe.

If the literal bytes `ee a4 1a 05 a5 11 02 16` appear in the .exe → the
trailer is a *fixed* per-build signature shipped with the firmware (so the
.exe just streams the .bin verbatim and the device validates against a
manufacturer key — patching is permanently blocked unless we extract that
key from the bootloader).

If they don't appear → the .exe computes the signature at upload time. The
algorithm lives in code we can reverse-engineer.

We also dump context around the 5× `/ZZZ` hits — those are inside resource
strings or compiled-in templates, and might tell us how upload is framed.
"""
from pathlib import Path

ROOT = Path("/Users/mbelleau/Code/usb_dsp_mac")
EXE  = (ROOT / "downloads/DSP-408-Windows/DSP-408-Windows-V1.24 190622/DSP-408.exe").read_bytes()
FW   = (ROOT / "downloads/DSP-408-Firmware.bin").read_bytes()

# The 8-byte signature lives at offset 0x11290 of the .bin
# (right after "/ZZZ" at 0x11288), followed by the 4-byte aa terminator
TRAILER_OFF = FW.find(b"/ZZZ")
print(f".bin /ZZZ marker at 0x{TRAILER_OFF:x}")
sig = FW[TRAILER_OFF + 4 : TRAILER_OFF + 12]
term = FW[TRAILER_OFF + 12 : TRAILER_OFF + 16]
print(f"  8-byte signature : {sig.hex(' ')}")
print(f"  4-byte terminator: {term.hex(' ')}")

print()
print("=== Looking for literal signature bytes in .exe ===")
def find_all(buf: bytes, needle: bytes) -> list[int]:
    out, pos = [], 0
    while True:
        i = buf.find(needle, pos)
        if i < 0: return out
        out.append(i); pos = i + 1

for needle, label in [
    (sig,                                  "8-byte signature"),
    (sig[:4],                              "first 4 bytes of sig"),
    (sig[4:],                              "last 4 bytes of sig"),
    (sig + term,                           "sig+term combined"),
    (b"/ZZZ" + sig,                        "/ZZZ + sig"),
    (b"/ZZZ" + sig + term,                 "/ZZZ + sig + term"),
    (b"\xee\xa4\x1a\x05",                  "first 4 bytes (raw)"),
    (b"\xa5\x11\x02\x16",                  "last 4 bytes (raw)"),
    (b"\xaa\x00\x00\x00",                  "0xAA terminator"),
]:
    h = find_all(EXE, needle)
    print(f"  {label:32s} hits: {len(h):4d}  first: {h[:4]}")

print()
print("=== Context around each /ZZZ hit in .exe ===")
for hit in find_all(EXE, b"/ZZZ"):
    slab = EXE[hit - 32 : hit + 64]
    hex_ = slab.hex(" ")
    txt  = "".join(chr(b) if 32 <= b < 127 else "." for b in slab)
    print(f"  @ 0x{hit:08x}:")
    # 3 rows of 32 bytes
    for r in range(0, len(slab), 32):
        row = slab[r:r+32]
        addr = hit - 32 + r
        rh = row.hex(" ")
        rt = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"     {addr:08x}  {rh}  |{rt}|")
    print()
