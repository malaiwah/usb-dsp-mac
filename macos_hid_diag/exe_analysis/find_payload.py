#!/usr/bin/env python3
"""Look for the actual firmware *payload* (STM32 vector table + .text) inside
DSP-408.exe.

The WMCU/MYDW-AV strings near 0x40eb28 are just template/format-string bytes
(notice the "1.00" placeholder vs. "1.06" in the actual .bin). The real
question is whether the *flashed image* (the post-WMCU-header bytes) is also
embedded — searched for via:

  - SP value 0x200049a0 (LE bytes a0 49 00 20) — entry of vector table
  - Reset handler 0x08005101 (LE 01 51 00 08) — second vector slot
  - "/ZZZ"   trailer marker
  - Long unique runs from the .bin (e.g. the first 32 bytes after the WMCU
    header) tested at every .exe offset

If the post-header bytes do NOT appear in the .exe, the .exe must either:
  (a) generate the WMCU container by reading a separate firmware file at
      runtime (we'd need to find which file),
  (b) decompress / decrypt it from a different region, or
  (c) the .exe never flashes — flashing happens entirely on-device after a
      raw upload, and the .exe just streams whatever is at firmware[0..end].
"""
from pathlib import Path

ROOT = Path("/Users/mbelleau/Code/usb_dsp_mac")
EXE = (ROOT / "downloads/DSP-408-Windows/DSP-408-Windows-V1.24 190622/DSP-408.exe").read_bytes()
FW = (ROOT / "downloads/DSP-408-Firmware.bin").read_bytes()


def find_all(buf: bytes, needle: bytes, limit: int = 8) -> list[int]:
    out, pos = [], 0
    while len(out) < limit:
        i = buf.find(needle, pos)
        if i < 0:
            break
        out.append(i)
        pos = i + 1
    return out


print(f".exe = {len(EXE):,}    .bin = {len(FW):,}\n")

# 1. Vector table fingerprints
sp_le  = (0x200049A0).to_bytes(4, "little")
rst_le = (0x08005101).to_bytes(4, "little")
print(f"SP LE bytes  {sp_le.hex(' ')}  hits in .exe: {find_all(EXE, sp_le)}")
print(f"RST LE bytes {rst_le.hex(' ')} hits in .exe: {find_all(EXE, rst_le)}")
print(f"SP+RST adjacent (8 bytes) in .exe: "
      f"{find_all(EXE, sp_le + rst_le)}")
print()

# 2. Long unique chunks from various regions of the .bin tested in .exe
def report(label: str, offset: int, length: int) -> None:
    chunk = FW[offset: offset + length]
    hits = find_all(EXE, chunk, 4)
    print(f"  {label:25s} fw[0x{offset:06x}..+{length:3d}]  hits: {hits}")
    if not hits:
        # try shorter prefixes to see how much of the chunk *does* match
        for L in (32, 16, 8):
            sub = chunk[:L]
            h = find_all(EXE, sub, 1)
            if h:
                print(f"     prefix len={L} matches at {h}")
                break

print("Test embedding of payload chunks (post-WMCU-header bytes):")
report("vector table (8B)",            0x08, 8)
report("vector table+ISRs (64B)",      0x08, 64)
report("text region @ 0x1000 (64B)",   0x1000, 64)
report("MYDW-AV string region (64B)",  0x3000, 64)
report("middle of payload (64B)",      0x8000, 64)
report("just before /ZZZ (64B)",       0x11248, 64)
report("/ZZZ + trailer (16B)",         0x11288, 16)
print()

# 3. Show the bytes around the .exe's WMCU template + a longer window
exe_wmcu = EXE.find(b"WMCU")
print(f"Bytes around .exe WMCU template @ 0x{exe_wmcu:x} (256-byte window):")
slab = EXE[exe_wmcu - 64: exe_wmcu + 192]
for i in range(0, len(slab), 32):
    row = slab[i: i + 32]
    addr = exe_wmcu - 64 + i
    hex_ = row.hex(" ")
    txt = "".join((chr(b) if 32 <= b < 127 else ".") for b in row)
    print(f"  {addr:08x}  {hex_}  |{txt}|")

# 4. Inspect first bytes of the .bin — header + first 64 bytes
print(f"\nFirst 96 bytes of .bin (raw):")
slab = FW[:96]
for i in range(0, len(slab), 32):
    row = slab[i: i + 32]
    hex_ = row.hex(" ")
    txt = "".join((chr(b) if 32 <= b < 127 else ".") for b in row)
    print(f"  {i:08x}  {hex_}  |{txt}|")
