#!/usr/bin/env python3
"""patch_firmware.py — produce two patched DSP-408 firmware images for the
"is the trailer actually verified?" experiment.

Output 1 — the actually-useful patch:
    DSP-408-Firmware-V6.21-PATCHED-hidpage.bin
    Single byte change: file[0xB8C0] : 0x8C → 0x0C
    Effect:   HID Report Descriptor "Usage Page" item changes from
              0x8C (Bar Code Scanner — uninteresting to macOS hidd)
              to   0x0C (Consumer Control — heavily handled by macOS hidd).
    Hope:     macOS finally delivers input reports to userspace.

Output 2 — the integrity-check control patch:
    DSP-408-Firmware-V6.21-PATCHED-noop.bin
    Single byte change: file[<benign offset>] : low bit flipped
    Effect:   None observable on the device behavior. The byte is in a
              region that holds zero/padding (no executable code, no
              constants used by the firmware).
    Purpose:  If THIS upload is rejected too, integrity is enforced
              regardless of where you patch (true cryptographic gate or
              whole-image checksum). If THIS upload succeeds but the
              hidpage upload fails, the .exe must be doing per-region
              validation. If both succeed, no integrity is enforced.

Outputs go in firmware_patch/. The original DSP-408-Firmware-V6.21.bin
stays in downloads/ as the recovery copy.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import sys

ROOT = Path(__file__).resolve().parent.parent
SRC  = ROOT / "downloads" / "DSP-408-Firmware-V6.21.bin"
OUT  = Path(__file__).resolve().parent

EXPECTED_SHA = (
    "97a4e23c315fbccdb5aff4cd5d6673643bb902b9138493b362df664581729268"
)

# ── Patch #1 — HID descriptor Usage Page byte ────────────────────────────
HIDPAGE_OFF      = 0xB8C0
HIDPAGE_OLD_BYTE = 0x8C    # Bar Code Scanner
HIDPAGE_NEW_BYTE = 0x0C    # Consumer Control

# ── Patch #2 — single-bit flip in benign padding ─────────────────────────
# The .bin between the trailer (/ZZZ at 0x11288 + 12 bytes = 0x11294) and
# the EOF (0x11298) is the 4-byte 0xAA terminator. Don't touch it.
# A safer benign target: the firmware contains many runs of 0x00 padding
# after the strings. Find one and flip the low bit of a single 0x00.
def find_benign_offset(data: bytes) -> int:
    """Return an offset that lives in a long zero-padding run, well away
    from code, the HID descriptor, the trailer, and the version string."""
    # Constraints:
    #   - At least 0x4000 bytes after the vector table (avoid .text)
    #   - At least 0x500 bytes before /ZZZ
    #   - Surrounded by ≥32 bytes of 0x00 on both sides
    #   - Not near the USB descriptor block (0xB880..0xB920)
    trailer = data.find(b"/ZZZ")
    forbidden = [(0xB860, 0xB920), (trailer - 16, trailer + 16)]
    for off in range(0x4000, trailer - 0x500):
        if any(lo <= off < hi for lo, hi in forbidden):
            continue
        window = data[off - 32: off + 33]
        if len(window) == 65 and all(b == 0 for b in window):
            return off
    raise SystemExit("no benign padding region found")


def patch(src_data: bytes, offset: int, new_byte: int, label: str) -> bytes:
    out = bytearray(src_data)
    old = out[offset]
    if old == new_byte:
        raise SystemExit(f"{label}: byte already {new_byte:#04x} at 0x{offset:x}")
    out[offset] = new_byte
    return bytes(out)


def write_patched(name: str, data: bytes, src: bytes,
                  offset: int, label: str) -> None:
    path = OUT / name
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    diff = [(i, src[i], data[i]) for i in range(len(src)) if src[i] != data[i]]
    print(f"\n  {label}")
    print(f"    file       : {path.relative_to(ROOT)}")
    print(f"    size       : {len(data):,} bytes (must equal source: "
          f"{len(data) == len(src)})")
    print(f"    sha256     : {sha}")
    print(f"    bytes diff : {len(diff)} (expect 1)")
    for off, b_old, b_new in diff:
        print(f"      offset 0x{off:x} : {b_old:#04x} → {b_new:#04x}")


def main() -> None:
    if not SRC.exists():
        sys.exit(f"missing {SRC}")
    src = SRC.read_bytes()
    sha = hashlib.sha256(src).hexdigest()
    if sha != EXPECTED_SHA:
        sys.exit(f"source sha256 mismatch — refuse to patch\n"
                 f"  got:      {sha}\n  expected: {EXPECTED_SHA}")

    print(f"source     : {SRC.relative_to(ROOT)}")
    print(f"size       : {len(src):,} bytes")
    print(f"sha256     : {sha}  ✓ matches expected V6.21\n")

    # Sanity-check the HID page byte location
    if src[HIDPAGE_OFF] != HIDPAGE_OLD_BYTE:
        sys.exit(f"unexpected byte at HID page offset 0x{HIDPAGE_OFF:x}: "
                 f"got 0x{src[HIDPAGE_OFF]:02x}, want "
                 f"0x{HIDPAGE_OLD_BYTE:02x}")
    print(f"HID Usage Page byte at 0x{HIDPAGE_OFF:x}: 0x{src[HIDPAGE_OFF]:02x} "
          f"(Bar Code Scanner) — verified.")
    print(f"HID Report Descriptor (33 bytes around 0xB8BF):")
    print(f"  {src[0xB8BF: 0xB8BF + 33].hex(' ')}\n")

    # Patch 1 — useful
    p1 = patch(src, HIDPAGE_OFF, HIDPAGE_NEW_BYTE, "hidpage")
    write_patched(
        "DSP-408-Firmware-V6.21-PATCHED-hidpage.bin", p1, src,
        HIDPAGE_OFF,
        "PATCH #1: HID Usage Page  0x8C (BarCodeScanner) → 0x0C (Consumer)"
    )

    # Patch 2 — control
    benign_off = find_benign_offset(src)
    print(f"\n  benign offset found at 0x{benign_off:x} "
          f"(byte = 0x{src[benign_off]:02x}, surrounded by 32+ zeros)")
    p2 = patch(src, benign_off, 0x01, "noop-control")
    write_patched(
        "DSP-408-Firmware-V6.21-PATCHED-noop.bin", p2, src,
        benign_off,
        "PATCH #2: NO-OP CONTROL  flip 1 byte in benign zero-padding"
    )

    # Triple-check the HID descriptor in patched file
    print("\nHID Report Descriptor in patched file (PATCH #1):")
    print(f"  {p1[0xB8BF: 0xB8BF + 33].hex(' ')}")
    print("HID Report Descriptor delta:")
    src_d = src[0xB8BF: 0xB8BF + 33]
    new_d = p1[0xB8BF: 0xB8BF + 33]
    for i, (a, b) in enumerate(zip(src_d, new_d)):
        if a != b:
            print(f"  desc[{i}] : 0x{a:02x} → 0x{b:02x}")


if __name__ == "__main__":
    main()
