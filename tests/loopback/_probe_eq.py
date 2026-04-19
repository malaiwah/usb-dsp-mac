"""Live-probe the EQ band write encoding via Scarlett loopback.

Capture analysis decoded:
  cmd     = 0x10000 + (band << 8) + channel    (e.g. 0x10101 = band 1, ch 1)
  payload = [freq_le16, gain_raw_le16, b4=0x34, 0, 0, 0]   (8 bytes)
  gain    raw = (dB × 10) + 600  (same as channel volume)
  freq    Hz, u16 LE

Three unknowns we tackle here:

1.  Does the write actually take effect? (round-trip via channel-state read)
2.  What is byte [4] (always 0x34=52 in capture — Q? filter type?)
3.  What does the response look like? (peaking? shelving? notch?)

Tests:
  A. Default (b4=0x34): freq=1000, gain=+12 dB.  Measure response shape.
  B. Halve b4 (0x1A=26):                          Measure shape; if narrower → Q
  C. Double b4 (0x68=104):                       Measure shape; if wider → Q
  D. Try b4=0 (unknown semantics)
  E. Try b4=1 (could be filter type if 0=peak/1=shelf/...)
"""
import sys, time
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
from audio_io import (
    DEFAULT_SR, mono_to_stereo, play_and_record, sine, tone_level_at,
)
from dsp408 import Device, enumerate_devices
from dsp408.protocol import CAT_PARAM, CMD_WRITE_EQ_BAND_BASE

SR = DEFAULT_SR
DSP_OUT_INDEX = 1     # OUT 2 (the only output wired back)
TONE_DUR_S = 0.20
TONE_AMP_DBFS = -20.0

# Probe frequencies — dense around 1 kHz to resolve a peaking-EQ bandwidth.
FREQS = [100, 200, 400, 600, 800, 900, 950, 1000, 1050, 1100, 1200, 1400,
         1700, 2000, 2500, 3000, 4000, 6000, 10000]


def write_eq_band(d, ch, band, freq, gain_db, b4):
    raw = max(0, min(1200, round(gain_db * 10 + 600)))  # ±60 dB clamp
    payload = bytes([
        freq & 0xFF, (freq >> 8) & 0xFF,
        raw & 0xFF, (raw >> 8) & 0xFF,
        b4, 0, 0, 0,
    ])
    cmd = CMD_WRITE_EQ_BAND_BASE + (band << 8) + ch
    d.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)


def setup(dsp):
    dsp.set_master(db=0.0, muted=False)
    for _ in range(8):
        dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False)
        time.sleep(0.05)
        dsp.read_channel_state(DSP_OUT_INDEX)
    for ch in range(8):
        if ch != DSP_OUT_INDEX:
            dsp.set_channel(ch, db=0.0, muted=True)
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
    dsp.set_routing(DSP_OUT_INDEX, in1=True, in2=False, in3=False, in4=False)
    # Wide-open crossover so it doesn't shape the response
    dsp.set_crossover(DSP_OUT_INDEX, 10, 0, 0, 22000, 0, 0)
    time.sleep(0.2)


def measure(freq):
    tone = sine(freq, TONE_DUR_S, amp_dbfs=TONE_AMP_DBFS)
    cap = play_and_record(mono_to_stereo(tone, left=True, right=False))
    n_lead = int(0.06 * SR); n_tail = int(0.04 * SR)
    body = slice(n_lead, len(cap) - n_tail)
    bw = max(8.0, freq * 0.02)
    ref = tone_level_at(cap.lp1[body], cap.sr, freq, bw_hz=bw)
    sig = tone_level_at(cap.in1[body], cap.sr, freq, bw_hz=bw)
    return sig - ref


def measure_curve(label):
    print(f"\n{label}")
    print("  freq Hz  |  gain dB rel ref")
    print("  ---------|------------------")
    out = []
    for f in FREQS:
        g = measure(f)
        out.append((f, g))
        print(f"  {f:>7}  |  {g:+7.2f}")
    return out


info = enumerate_devices()[0]
with Device.open(path=info["path"]) as dsp:
    dsp.connect()
    setup(dsp)

    # Establish baseline: read channel state with all bands at default
    blob_default = bytes(dsp.read_channel_state(DSP_OUT_INDEX))
    print(f"channel state (default) — first 80 bytes (10 bands × 8b):")
    for i in range(10):
        b = blob_default[i*8:(i+1)*8]
        freq = int.from_bytes(b[0:2], "little")
        gain_raw = int.from_bytes(b[2:4], "little")
        gain_db = (gain_raw - 600) / 10.0
        print(f"  band {i}: {b.hex()}  freq={freq:>5} Hz  gain={gain_db:+.1f} dB  b4={b[4]:#04x}")

    baseline = measure_curve("=== Baseline: all bands flat (default) ===")

    # Test A: peaking +12 dB at 1 kHz with b4=0x34 (default Q)
    # Use band 5 (default freq=1000 Hz so we're not moving it)
    write_eq_band(dsp, DSP_OUT_INDEX, band=5, freq=1000, gain_db=+12, b4=0x34)
    time.sleep(0.4)

    # Verify round-trip
    blob_a = bytes(dsp.read_channel_state(DSP_OUT_INDEX))
    band5_a = blob_a[5*8:(5+1)*8]
    print(f"\nafter write band5 (+12 dB, b4=0x34): {band5_a.hex()}")

    test_a = measure_curve("=== Test A: band5 +12 dB @ 1000 Hz, b4=0x34 ===")

    # Test B: same gain, b4=0x1A (half — narrower Q if b4 is Q)
    write_eq_band(dsp, DSP_OUT_INDEX, band=5, freq=1000, gain_db=+12, b4=0x1A)
    time.sleep(0.4)
    blob_b = bytes(dsp.read_channel_state(DSP_OUT_INDEX))
    print(f"\nafter b4=0x1A: {blob_b[5*8:(5+1)*8].hex()}")
    test_b = measure_curve("=== Test B: band5 +12 dB @ 1000 Hz, b4=0x1A (half) ===")

    # Test C: b4=0x68 (double — wider Q if b4 is Q)
    write_eq_band(dsp, DSP_OUT_INDEX, band=5, freq=1000, gain_db=+12, b4=0x68)
    time.sleep(0.4)
    blob_c = bytes(dsp.read_channel_state(DSP_OUT_INDEX))
    print(f"\nafter b4=0x68: {blob_c[5*8:(5+1)*8].hex()}")
    test_c = measure_curve("=== Test C: band5 +12 dB @ 1000 Hz, b4=0x68 (double) ===")

    # Restore band5 to defaults: freq=1000, gain=0, b4=0x34
    write_eq_band(dsp, DSP_OUT_INDEX, band=5, freq=1000, gain_db=0, b4=0x34)
    time.sleep(0.3)
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)

# Summary
print("\n=== Summary (relative to baseline) ===")
print("  freq    | base    | A b4=0x34 | B b4=0x1A | C b4=0x68 |")
print("  --------|---------|-----------|-----------|-----------|")
for i, (f, gb) in enumerate(baseline):
    ga = test_a[i][1] - gb
    gbb = test_b[i][1] - gb
    gc = test_c[i][1] - gb
    print(f"  {f:>5} Hz | {gb:+6.2f} | {ga:+8.2f}  | {gbb:+8.2f}  | {gc:+8.2f}  |")
