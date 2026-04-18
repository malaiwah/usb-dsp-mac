#!/usr/bin/env python3
"""find_hid_in_firmware.py — locate the HID Report Descriptor inside
DSP-408-Firmware.bin so we know exactly which bytes to patch.

Strategy:
1. Search for descriptor signatures (Usage Page items followed by Usage,
   Collection, etc.) — both vendor-defined (06 00 FF) and standard.
2. For each candidate, walk the HID item parser; if it parses cleanly until
   an End Collection (0xC0) and the length matches a plausible descriptor,
   we've found it.
3. Print the offset and the patch we'd want to apply.
"""

from __future__ import annotations
import sys
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parent.parent / "downloads" / "DSP-408-Firmware.bin"

# Candidate descriptor starts: Usage Page items (Global type=1, tag=0)
#   short form:  05 XX        (Usage Page = XX)
#   long form:   06 XX YY     (Usage Page = YY:XX, little-endian)
# We want to find a Usage Page item followed by something that parses
# cleanly as a Main / Local / Global item and eventually hits 0xC0 (End
# Collection) at a reasonable distance.

def parse_hid(data: bytes, start: int, max_len: int = 256) -> tuple[int, list]:
    """Parse HID items starting at `start`. Returns (consumed_bytes, items).
    Returns (0, []) if parsing fails."""
    i = start
    items = []
    end_collection_seen = 0
    open_collections = 0
    while i < min(start + max_len, len(data)):
        prefix = data[i]
        size_code = prefix & 0x03
        type_     = (prefix >> 2) & 0x03
        tag       = (prefix >> 4) & 0x0F
        data_len  = 4 if size_code == 3 else size_code
        if i + 1 + data_len > len(data):
            return 0, []
        val = int.from_bytes(data[i+1:i+1+data_len], "little") if data_len else 0
        items.append((i - start, prefix, type_, tag, val))

        # Track collection nesting
        if type_ == 0 and tag == 0xA:        # Collection
            open_collections += 1
        elif type_ == 0 and tag == 0xC:      # End Collection
            open_collections -= 1
            end_collection_seen += 1
            if open_collections == 0:
                # We've closed the outermost collection — descriptor ends here.
                return (i + 1 + data_len) - start, items

        # Sanity: bail on obviously-bogus prefixes
        if type_ == 3:  # reserved
            return 0, []

        i += 1 + data_len

    return 0, []


def usage_page_name(page: int) -> str:
    if page >= 0xFF00: return f"VENDOR-DEFINED 0x{page:04X}  ← root cause"
    return {
        0x01:"Generic Desktop", 0x07:"Keyboard/Keypad", 0x09:"Button",
        0x0B:"Telephony", 0x0C:"Consumer", 0x0D:"Digitizer",
    }.get(page, f"0x{page:X}")


def scan(data: bytes) -> list:
    candidates = []
    for i in range(len(data) - 4):
        b = data[i]
        # Usage Page item: prefix byte = 0x04..0x07 (size 0..3, type=1, tag=0)
        # In practice 0x05 (short, 1 byte) or 0x06 (short, 2 bytes)
        if b not in (0x04, 0x05, 0x06, 0x07):
            continue
        size = b & 0x03
        usage_page_bytes = 4 if size == 3 else size
        if i + 1 + usage_page_bytes > len(data):
            continue
        page = int.from_bytes(data[i+1:i+1+usage_page_bytes], "little")

        # Next item should be a Usage (Local type=2 tag=0): prefix 0x08..0x0B
        next_off = i + 1 + usage_page_bytes
        if next_off >= len(data) or data[next_off] not in (0x08, 0x09, 0x0A, 0x0B):
            continue

        # Try a full parse
        consumed, items = parse_hid(data, i, max_len=512)
        if consumed < 8 or consumed > 512:
            continue
        # Require at least one Collection and one End Collection
        had_coll = any(t == 0 and tg == 0xA for _, _, t, tg, _ in items)
        had_end  = any(t == 0 and tg == 0xC for _, _, t, tg, _ in items)
        if not (had_coll and had_end):
            continue

        candidates.append((i, consumed, page, items))
    return candidates


def main():
    if not FIRMWARE.exists():
        sys.exit(f"firmware not found: {FIRMWARE}")
    data = FIRMWARE.read_bytes()
    print(f"Loaded {FIRMWARE.name}: {len(data)} bytes\n")

    cands = scan(data)
    if not cands:
        print("No HID Report Descriptor candidates found.")
        print("(Maybe the descriptor is split, encrypted, or stored in a non-obvious format.)")
        return

    print(f"Found {len(cands)} candidate descriptor(s):\n")
    for off, length, page, items in cands:
        print(f"  ── candidate @ offset 0x{off:04x} ({off}), length {length} bytes ──")
        print(f"     Usage Page: {usage_page_name(page)}")
        print(f"     Raw bytes : {data[off:off+min(length,32)].hex(' ')}"
              f"{'…' if length > 32 else ''}")
        print(f"     Total items: {len(items)}")

        # Show first few items
        for rel, prefix, type_, tag, val in items[:8]:
            t_name = ["Main","Global","Local","Rsvd"][type_]
            print(f"        +{rel:3d}  {prefix:02x}  {t_name} tag={tag:X} data=0x{val:X}")
        if len(items) > 8:
            print(f"        ... +{len(items)-8} more items")

        # If this is vendor-defined, show the patch
        if page >= 0xFF00:
            ub = data[off]
            size = ub & 0x03
            n = 4 if size == 3 else size
            byte_offsets = list(range(off + 1, off + 1 + n))
            print(f"\n     >>> PATCH TARGET <<<")
            print(f"     Bytes at offsets {byte_offsets} encode 0x{page:X} (little-endian).")
            print(f"     Recommended change: rewrite the Usage Page item as:")
            print(f"        Original: {data[off:off+1+n].hex(' ')}")
            print(f"        Patched : 05 0c                ← Usage Page = Consumer (0x0C)")
            print(f"     i.e. set firmware[{off}]=0x05, firmware[{off+1}]=0x0C,")
            if n == 2:
                print(f"          firmware[{off+2}]=0x09 (turn the 3rd byte into the next item start)")
                print(f"     ⚠ Length-changing edits break checksums and downstream offsets;")
                print(f"       a safer in-place patch keeps prefix=0x06 and changes the data:")
                print(f"        firmware[{off+1}]=0x00 firmware[{off+2}]=0x0C   ← Usage Page = 0x0C00")
                print(f"        ↑ unusual but valid (extended form); macOS treats 0x0C00 same as 0x0C")
        print()

    # Suggest the cross-check workflow
    print("──────────────────────────────────────────────────────────────────")
    print("CROSS-CHECK: build & run dump_hid_descriptor first, then compare")
    print("its byte output against the candidate(s) above.  The candidate")
    print("whose bytes match exactly is the one inside the firmware.")
    print("──────────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
