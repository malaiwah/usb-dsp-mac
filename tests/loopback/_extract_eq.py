"""Pull every cmd=0x10000..0x10FFF (EQ band) WRITE frame from the
full-sequence capture and dump payload byte variations.

Goal: figure out what byte [4] really is (Q? filter type? bandwidth?)
by seeing if it ever varies, and if so, against what other change."""
import subprocess
out = subprocess.check_output([
    "tshark", "-r", "/tmp/full-sequence.pcapng",
    "-T", "fields",
    "-e", "frame.number", "-e", "frame.time_relative",
    "-e", "usbhid.data",
], text=True)

eq_frames = []  # (fnum, t, dir, cat, cmd, plen, payload_hex)
for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) < 3 or not parts[2]:
        continue
    fnum, t, hexd = parts
    raw = bytes.fromhex(hexd)
    if len(raw) < 14: continue
    if raw[:4] != b"\x80\x80\x80\xee": continue
    direction = raw[4]
    cat = raw[7]
    cmd = int.from_bytes(raw[8:12], "little")
    plen = int.from_bytes(raw[12:14], "little")
    payload = raw[14:14 + min(plen, 32)]
    if 0x10000 <= cmd <= 0x10FFF:
        eq_frames.append((fnum, float(t), direction, cat, cmd, plen, payload))

print(f"Found {len(eq_frames)} EQ-range frames\n")
print(f"{'frame':>6} {'t':>7} {'dir':>4} {'cat':>4} {'cmd':>8} {'len':>4} payload")
print("-" * 80)
seen_writes = {}  # cmd → list of payloads
for f, t, d, c, cmd, l, p in eq_frames:
    if d == 0xa1:  # WRITE only
        seen_writes.setdefault(cmd, []).append((f, t, p))
    print(f"{f:>6} {t:>7.2f} {d:#04x} {c:#04x} {cmd:#08x} {l:>4} {p.hex()}")

print("\n=== Per-cmd write payload distinct values ===")
for cmd in sorted(seen_writes):
    band = (cmd >> 8) & 0xFF
    chan = cmd & 0xFF
    plds = seen_writes[cmd]
    distinct = sorted({p.hex() for _, _, p in plds})
    print(f"  cmd={cmd:#07x}  (band={band} ch={chan}, n={len(plds)}, "
          f"distinct={len(distinct)})")
    for hx in distinct:
        print(f"    {hx}")

# Decode each distinct payload
print("\n=== Decoded ===")
for cmd in sorted(seen_writes):
    band = (cmd >> 8) & 0xFF
    chan = cmd & 0xFF
    distinct = sorted({p.hex() for _, _, p in seen_writes[cmd]})
    for hx in distinct:
        raw = bytes.fromhex(hx)
        if len(raw) < 8:
            print(f"  cmd={cmd:#07x}  short payload {hx}")
            continue
        freq = int.from_bytes(raw[0:2], "little")
        gain_raw = int.from_bytes(raw[2:4], "little")
        gain_db = (gain_raw - 600) / 10.0
        b4 = raw[4]
        b5_7 = raw[5:8]
        # Try interpreting [4..5] as LE16 too
        b45 = int.from_bytes(raw[4:6], "little")
        print(f"  band={band} ch={chan}  freq={freq:>5} Hz  "
              f"gain={gain_db:+5.1f}dB(raw={gain_raw})  "
              f"b4={b4:#04x}({b4})  b45_le={b45}  trail={b5_7.hex()}")
