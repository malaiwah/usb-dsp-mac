"""Test whether DSP-408 routing cells are u8 percentage or boolean.

Open question since session start. The Windows GUI only ever sends 0x00
or 0x64 (= 100 dec). The leon Android app code claims they're percentages.
Empirical test: write 0x32 (= 50 dec) and 0x14 (= 20 dec) to a routing
cell and see if the audio level scales accordingly.

Setup: same as test_gain_calibration.py — Scarlett OUT 1 → DSP IN → OUT 2 → IN 1.

Expectation if PERCENTAGE encoding (linear amplitude):
    routing[0] = 0x64 (100) → reference level (e.g. -19 dBFS)
    routing[0] = 0x32 (50)  → reference -6 dB
    routing[0] = 0x14 (20)  → reference -14 dB (= 20*log10(0.2))
    routing[0] = 0x0A (10)  → reference -20 dB
    routing[0] = 0x00       → silence

If PERCENTAGE encoded as dB×10 (like channel volume):
    Then 0x64 = 100 might mean +10 dB, 0x32 = +5 dB, etc. Different curve.

If BOOLEAN:
    0x64 → reference, anything else → silence (or weird).

We'll write each value via write_raw (bypassing set_routing's bool API)
and measure the resulting level.

    sg audio -c '.venv/bin/python tests/loopback/test_routing_percentage.py'
"""
from __future__ import annotations

import math
import sys
import time

sys.path.insert(0, "/home/mbelleau/dsp408")

import numpy as np
from audio_io import (
    DEFAULT_SR,
    PLAYBACK_CHANNELS,
    play_and_record,
    sine,
    tone_level_at,
)

from dsp408 import Device, enumerate_devices
from dsp408.protocol import CAT_PARAM, CMD_ROUTING_BASE

TONE_HZ = 1000.0
TONE_AMP_DBFS = -20.0
TONE_DUR_S = 0.4
SCARLETT_OUT_IDX = 0    # Scarlett OUT 1
DSP_OUT_INDEX = 1       # OUT 2 (the only output wired back to Scarlett IN 1)
DSP_IN_INDEX = 0        # IN 1 — payload byte index for the routing cell

# Sweep values: powers of 2 + the official "ON" / "OFF" + intermediate %s
SWEEP_VALUES = [
    (0x00, "OFF"),
    (0x05, "5"),
    (0x0A, "10"),
    (0x14, "20"),
    (0x19, "25"),
    (0x32, "50"),
    (0x4B, "75"),
    (0x64, "100 (ON)"),
    (0x80, "128 (>100)"),
    (0xC8, "200 (>>100)"),
    (0xFF, "255 (max u8)"),
]


def play_one_scarlett_out(out_idx: int):
    tone = sine(TONE_HZ, TONE_DUR_S, amp_dbfs=TONE_AMP_DBFS)
    block = np.zeros((len(tone), PLAYBACK_CHANNELS), dtype=np.float64)
    block[:, out_idx] = tone
    return play_and_record(block, sr=DEFAULT_SR)


def measure(cap) -> float:
    n_lead = int(0.05 * cap.sr)
    n_tail = int(0.05 * cap.sr)
    body = slice(n_lead, len(cap) - n_tail)
    return tone_level_at(cap.in1[body], cap.sr, TONE_HZ)


def write_routing_raw(dsp: Device, output_idx: int, in_levels: list[int]) -> None:
    """Write a routing row with arbitrary u8 levels (bypassing set_routing's
    boolean abstraction).

    in_levels[0..3] = level for IN1..IN4 (any byte value, not just 0/100).
    """
    payload = bytes(in_levels[:4]) + bytes(4)  # pad to 8 bytes
    cmd = CMD_ROUTING_BASE + output_idx
    dsp.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)


def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found")
    info = devs[0]
    print(f"DSP: {info['display_id']} path={info['path']!r}")
    print()

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        # Master at unity, all channels unmuted, no delay
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
        # All routing OFF as baseline
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        time.sleep(0.1)

        # Establish reference at routing = 0x64 (the "ON" value we always use)
        in_levels = [0] * 4
        in_levels[DSP_IN_INDEX] = 0x64
        write_routing_raw(dsp, DSP_OUT_INDEX, in_levels)
        time.sleep(0.05)
        cap = play_one_scarlett_out(SCARLETT_OUT_IDX)
        ref_dbfs = measure(cap)
        print(f"Reference (routing[IN{DSP_IN_INDEX+1}] = 0x64 = 100): "
              f"{ref_dbfs:+6.1f} dBFS")
        print()
        print(f"  {'value':>14s} {'measured':>10s} {'delta':>9s}  "
              f"{'expected if linear %':>24s}  {'expected if dB×10':>20s}")
        print(f"  {'─'*14} {'─'*10} {'─'*9}  {'─'*24}  {'─'*20}")

        results = []
        for raw_val, label in SWEEP_VALUES:
            in_levels[DSP_IN_INDEX] = raw_val
            write_routing_raw(dsp, DSP_OUT_INDEX, in_levels)
            time.sleep(0.05)
            cap = play_one_scarlett_out(SCARLETT_OUT_IDX)
            meas = measure(cap)
            delta = meas - ref_dbfs

            # Expected delta if it's a linear amplitude percentage:
            #     ratio = val / 100   →  delta_dB = 20*log10(ratio)
            # Expected delta if it's dB × 10 (sign convention TBD):
            #     could be (val - 100) / 10 ... or ((val - 100)/10 attenuation only)
            #     We'll print the simple "(val - 100) / 10" assumption.
            if raw_val == 0:
                exp_lin = "−∞"
            else:
                exp_lin = f"{20*math.log10(raw_val/100.0):+6.1f} dB"
            exp_db10 = f"{(raw_val - 100)/10:+6.1f} dB"
            results.append((raw_val, meas, delta, exp_lin, exp_db10))
            print(f"  0x{raw_val:02x} ({label:>7s}) {meas:+10.1f} {delta:+9.1f}  "
                  f"{exp_lin:>24s}  {exp_db10:>20s}")

        # Cleanup
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)

    # Verdict
    print()
    print("Interpretation:")
    print("  • If 'delta' tracks 'expected if linear %' → routing IS a u8 percentage")
    print("    (mixer cells are levels, our boolean API is too restrictive)")
    print("  • If 'delta' is 0 dB for non-zero values, -inf for zero → routing is BOOLEAN")
    print("    (the firmware just treats anything > 0 as 'on' at unity)")
    print("  • If 'delta' tracks (val-100)/10 → routing is a dB attenuator like")
    print("    channel volume (unlikely but possible)")
    print("  • If values > 100 give positive 'delta' → headroom above unity exists!")
    print("    (would mean routing supports gain BOOST per cell up to +X dB)")


if __name__ == "__main__":
    main()
