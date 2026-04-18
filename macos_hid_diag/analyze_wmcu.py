#!/usr/bin/env python3
"""analyze_wmcu.py — figure out the layout of the "WMCU" firmware container
shipped with the DSP-408 so we know exactly which bytes are header, payload,
and checksum/signature, and whether a 1-byte patch can be made flashable.

Goals:
1. Decode the 4-byte magic + 4-byte header field at offsets 0..7.
2. Locate the firmware vector table and confirm the payload boundary.
3. Identify the trailer ("/ZZZ" + tail bytes) and try every plausible
   checksum algorithm (CRC32 in many flavours, CRC16, sum, Adler-32) over
   every plausible byte range to find what produces the observed trailer.
4. Print whether a 1-byte patch at firmware[0xb8c0] = 0x0c is safe to
   reflash, and what (if anything) we'd have to recompute.
"""

from __future__ import annotations
import binascii
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW   = ROOT / "downloads" / "DSP-408-Firmware.bin"

# Patch we plan to apply (from dump_hid_descriptor + find_hid_in_firmware)
PATCH_OFFSET = 0xB8C0       # the 0x8C byte after the 0x05 Usage Page prefix
PATCH_NEW    = 0x0C         # Consumer Control instead of Bar Code Scanner


def hexdump(data: bytes, base: int = 0, max_lines: int = 4) -> str:
    out = []
    for i in range(0, min(len(data), max_lines*16), 16):
        chunk = data[i:i+16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {base+i:08x}  {hexpart:<47}  {ascii_}")
    return "\n".join(out)


def is_stm32_vector_table(data: bytes, off: int) -> bool:
    """A real STM32 vector table starts with:
       - initial SP in 0x20000000..0x20100000  (RAM)
       - reset vector in 0x08000000..0x080FFFFF (flash) with bit 0 set (Thumb)
    """
    if off + 8 > len(data): return False
    sp, rv = struct.unpack_from("<II", data, off)
    sp_ok = 0x20000000 <= sp <= 0x20100000
    rv_ok = 0x08000000 <= rv <= 0x080FFFFF and (rv & 1) == 1
    return sp_ok and rv_ok


def try_crc_algorithms(payload: bytes, expected: bytes, label: str) -> list[str]:
    """Test a battery of checksums over `payload`; return list of matching algo
    descriptions if the result equals `expected`."""
    matches = []

    # 32-bit candidates
    if len(expected) >= 4:
        exp32 = expected[:4]

        # CRC32 (zlib / IEEE 802.3, reflected)
        c = zlib.crc32(payload) & 0xFFFFFFFF
        for endian, packed in [("LE", struct.pack("<I", c)),
                               ("BE", struct.pack(">I", c))]:
            if packed == exp32:
                matches.append(f"CRC32 (IEEE/zlib) {endian} = 0x{c:08x}")

        # CRC32 inverted
        ci = (~c) & 0xFFFFFFFF
        for endian, packed in [("LE", struct.pack("<I", ci)),
                               ("BE", struct.pack(">I", ci))]:
            if packed == exp32:
                matches.append(f"~CRC32 (inverted) {endian} = 0x{ci:08x}")

        # 32-bit sum
        s = sum(payload) & 0xFFFFFFFF
        for endian, packed in [("LE", struct.pack("<I", s)),
                               ("BE", struct.pack(">I", s))]:
            if packed == exp32:
                matches.append(f"sum32 {endian} = 0x{s:08x}")

        # Adler-32
        a = zlib.adler32(payload) & 0xFFFFFFFF
        for endian, packed in [("LE", struct.pack("<I", a)),
                               ("BE", struct.pack(">I", a))]:
            if packed == exp32:
                matches.append(f"Adler-32 {endian} = 0x{a:08x}")

        # XOR-fold of all 32-bit words
        x = 0
        for i in range(0, len(payload) - (len(payload) % 4), 4):
            x ^= struct.unpack_from("<I", payload, i)[0]
        for endian, packed in [("LE", struct.pack("<I", x)),
                               ("BE", struct.pack(">I", x))]:
            if packed == exp32:
                matches.append(f"XOR32 fold {endian} = 0x{x:08x}")

    # 16-bit candidates
    if len(expected) >= 2:
        exp16 = expected[:2]
        s16 = sum(payload) & 0xFFFF
        for endian, packed in [("LE", struct.pack("<H", s16)),
                               ("BE", struct.pack(">H", s16))]:
            if packed == exp16:
                matches.append(f"sum16 {endian} = 0x{s16:04x}")

    # Single-byte sum / xor
    if len(expected) >= 1:
        sb = sum(payload) & 0xFF
        if bytes([sb]) == expected[:1]:
            matches.append(f"sum8 = 0x{sb:02x}")
        xb = 0
        for b in payload: xb ^= b
        if bytes([xb]) == expected[:1]:
            matches.append(f"xor8 = 0x{xb:02x}")

    # Cryptographic / 8-byte candidates
    if len(expected) >= 8:
        import hashlib
        for algo in ("md5", "sha1", "sha256", "sha512", "blake2s"):
            h = hashlib.new(algo, payload).digest()
            for endian, slc in [("first 8",  h[:8]),
                                ("last 8",   h[-8:])]:
                if slc == expected[:8]:
                    matches.append(f"{algo} ({endian}) match")

        # CRC-64 (Jones)
        try:
            import crc; del crc  # only if user has the lib
        except ImportError:
            pass
        # Manual CRC-64-XZ
        def crc64_xz(data: bytes) -> int:
            poly = 0xC96C5795D7870F42
            crc = 0xFFFFFFFFFFFFFFFF
            for b in data:
                crc ^= b
                for _ in range(8):
                    crc = (crc >> 1) ^ (poly & -(crc & 1))
                    crc &= 0xFFFFFFFFFFFFFFFF
            return crc ^ 0xFFFFFFFFFFFFFFFF
        c64 = crc64_xz(payload)
        for endian, packed in [("LE", struct.pack("<Q", c64)),
                               ("BE", struct.pack(">Q", c64))]:
            if packed == expected[:8]:
                matches.append(f"CRC-64-XZ {endian} = 0x{c64:016x}")

    return matches


def main():
    if not FW.exists(): sys.exit(f"firmware not found: {FW}")
    raw = FW.read_bytes()
    n = len(raw)
    print(f"Loaded {FW.name}: {n} bytes (0x{n:x})\n")

    # ── 1. Header ──────────────────────────────────────────────────────────
    print("=" * 70)
    print(" 1.  Header")
    print("=" * 70)
    print(hexdump(raw[:32]))
    magic = raw[:4]
    if magic == b"WMCU":
        print(f"\n  magic[0:4]  = b'WMCU'   ✓ confirmed")
    else:
        print(f"\n  magic[0:4]  = {magic!r}   ✗ unexpected")
    h47 = struct.unpack("<I", raw[4:8])[0]
    print(f"  hdr[4:8]    = 0x{h47:08x} (LE)  /  0x{struct.unpack('>I',raw[4:8])[0]:08x} (BE)")
    # Common header fields:
    #   - payload length (would equal n-8 or n-(8+trailer))
    #   - load address (would equal 0x08000000+offset)
    #   - format version
    candidates = []
    if h47 == n - 8:           candidates.append(f"= file_length - 8 (header) = {n-8}")
    if h47 == n:               candidates.append(f"= file_length total = {n}")
    if h47 == n - 24:          candidates.append(f"= file_length - 24 (header+trailer) = {n-24}")
    if h47 & 0xFF000000 == 0x08000000: candidates.append("looks like an STM32 flash address")
    if not candidates:
        # Maybe split: 2 shorts?
        a, b = struct.unpack("<HH", raw[4:8])
        candidates.append(f"as 2x uint16 LE: 0x{a:04x} ({a}), 0x{b:04x} ({b})")
    print(f"  interpretations:")
    for c in candidates: print(f"    • {c}")

    # ── 2. Payload boundary via vector table ───────────────────────────────
    print("\n" + "=" * 70)
    print(" 2.  Payload start (via STM32 vector-table scan)")
    print("=" * 70)
    payload_start = None
    for off in (4, 8, 12, 16, 32):
        if is_stm32_vector_table(raw, off):
            sp, rv = struct.unpack_from("<II", raw, off)
            print(f"  vector table at offset 0x{off:x}: SP=0x{sp:08x}  RESET=0x{rv:08x}  ✓")
            payload_start = off
            break
    if payload_start is None:
        print("  no vector table found at common offsets — payload structure unknown")
        payload_start = 8

    # ── 3. Trailer ─────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(" 3.  Trailer")
    print("=" * 70)
    tail = raw[-32:]
    print(hexdump(tail, base=n-32))

    # Look for "/ZZZ" delimiter
    zzz_idx = raw.rfind(b"/ZZZ")
    if zzz_idx >= 0:
        print(f"\n  '/ZZZ' delimiter at offset 0x{zzz_idx:x} ({zzz_idx})")
        sig = raw[zzz_idx + 4:]
        print(f"  bytes after '/ZZZ': {sig.hex(' ')}  ({len(sig)} bytes)")
        payload_end = zzz_idx
    else:
        print("  no '/ZZZ' marker found — trailer location unknown")
        payload_end = n

    # Possible terminator
    term = raw[-4:]
    if term == b"\xaa\x00\x00\x00":
        print(f"  terminator [-4:] = aa 00 00 00  ✓ likely magic end-of-file marker")

    # ── 4. Checksum / signature search ─────────────────────────────────────
    print("\n" + "=" * 70)
    print(" 4.  Checksum search")
    print("=" * 70)
    print(f"  payload candidate range: [{payload_start}, {payload_end})  ({payload_end-payload_start} bytes)")

    # Try the 12 bytes between '/ZZZ' and the aa 00 00 00 terminator as the checksum
    if zzz_idx > 0:
        cksum_candidates = [
            ("between /ZZZ and aa-terminator", raw[zzz_idx+4:n-4]),
            ("first 4 bytes after /ZZZ",       raw[zzz_idx+4:zzz_idx+8]),
            ("last 4 before aa-terminator",    raw[n-8:n-4]),
            ("entire 16-byte trailer",         raw[n-16:n]),
        ]
    else:
        cksum_candidates = [("last 4 bytes", raw[-4:])]

    payload_ranges = [
        ("everything except 8-byte header", raw[8:]),
        ("everything except header AND trailer", raw[8:zzz_idx] if zzz_idx>0 else raw[8:n-16]),
        ("everything except header AND /ZZZ+sig+term", raw[8:zzz_idx] if zzz_idx>0 else raw[8:]),
        ("payload only (vector table to /ZZZ)", raw[payload_start:zzz_idx] if zzz_idx>0 else raw[payload_start:]),
        ("entire file except last 4 bytes",  raw[:-4]),
        ("entire file except last 16 bytes", raw[:-16]),
        ("entire file except trailer (/ZZZ onward)", raw[:zzz_idx] if zzz_idx>0 else raw),
    ]

    found_any = False
    for cs_label, cs_bytes in cksum_candidates:
        for r_label, r_bytes in payload_ranges:
            matches = try_crc_algorithms(r_bytes, cs_bytes, r_label)
            for m in matches:
                print(f"  ✓ MATCH: trailer({cs_label}) == {m}  over({r_label})")
                found_any = True
    if not found_any:
        print("  ✗ no checksum algorithm matched any (range, trailer) combination.")
        print("    The trailer is most likely a CRYPTOGRAPHIC SIGNATURE (HMAC,")
        print("    truncated SHA, or similar) — patching the firmware will produce")
        print("    a binary the bootloader rejects. Verification needed before flashing.")

    # ── 5. Patch impact ────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(" 5.  Patch impact analysis")
    print("=" * 70)
    print(f"  Planned patch: firmware[0x{PATCH_OFFSET:x}] = 0x{PATCH_NEW:02x}  "
          f"(was 0x{raw[PATCH_OFFSET]:02x})")
    print(f"  Patch location is inside payload region [{payload_start}, {payload_end}): ", end="")
    if payload_start <= PATCH_OFFSET < payload_end: print("✓")
    else: print("✗ — patch lands outside payload, abort")

    if found_any:
        print("\n  → CRC formula is known. After patching, recompute and rewrite trailer bytes.")
        print("    Code to do this lives in the matched algorithm above.")
    else:
        print("\n  → Trailer integrity is unverified. Two options:")
        print("    a) Try flashing the patched .bin in the Windows app and see if the")
        print("       installer rejects it (the safest 'is it signed?' test).")
        print("    b) Decompile the bootloader from the early payload bytes to find")
        print("       the verification routine and confirm before flashing.")


if __name__ == "__main__":
    main()
