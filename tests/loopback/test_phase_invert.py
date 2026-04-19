"""Test the per-channel ``polar`` (phase invert) field of the DSP-408.

The 8-byte basic-record write payload (cmd=0x1FNN) has byte[1] currently
hardcoded to 0 in our driver. Per the leon Android-app analysis and our
296-byte blob decode, that byte is the phase-invert flag (polar). We've
verified it's at blob[247] live, but never actually written non-zero to it.

This test:
  1. Plays a 1 kHz tone via Scarlett OUT 1.
  2. Captures both Scarlett LP1 (= reference, what we played) and IN1
     (= signal that went through the DSP).
  3. Computes the phase relationship between IN1 and LP1.
  4. Toggles polar (writes byte[1] = 0 vs byte[1] = 1) on the DSP output
     channel that's wired back to the Scarlett.
  5. If polar works, the phase difference should flip 180° (= π radians)
     between the two cases.

Setup: Scarlett OUT 1 → DSP IN → DSP routes IN1→OUT2 → Scarlett IN1.

Run:
    sg audio -c '.venv/bin/python tests/loopback/test_phase_invert.py'
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
)

from dsp408 import Device, enumerate_devices
from dsp408.protocol import (
    CAT_PARAM,
    CHANNEL_SUBIDX,
    CHANNEL_VOL_OFFSET,
    CMD_WRITE_CHANNEL_BASE,
)

TONE_HZ = 1000.0
TONE_AMP_DBFS = -20.0
TONE_DUR_S = 0.4
SCARLETT_OUT_IDX = 0
DSP_OUT_INDEX = 1     # OUT 2 (the only one wired back)
DSP_IN_FOR_ROUTE = 1


def play_one_scarlett_out(out_idx: int):
    tone = sine(TONE_HZ, TONE_DUR_S, amp_dbfs=TONE_AMP_DBFS)
    block = np.zeros((len(tone), PLAYBACK_CHANNELS), dtype=np.float64)
    block[:, out_idx] = tone
    return play_and_record(block, sr=DEFAULT_SR)


def write_basic_record_with_polar(dsp: Device, channel: int,
                                   db: float, muted: bool, polar: bool,
                                   delay_samples: int = 0,
                                   spk_type: int | None = None) -> None:
    """Write the 8-byte cmd=0x1FNN payload with explicit polar control.

    Bypasses Device.set_channel() (which always writes polar=0) so we can
    flip byte[1] for this test. If empirically validated, this should be
    folded back into Device.set_channel() as a `polar=False` kwarg.
    """
    if not 0 <= channel <= 7:
        raise ValueError(f"channel must be in 0..7, got {channel}")
    si = (spk_type if spk_type is not None
          else CHANNEL_SUBIDX[channel])
    raw_vol = max(0, min(600, round(db * 10 + CHANNEL_VOL_OFFSET)))
    en_bit = 0 if muted else 1
    pol_bit = 1 if polar else 0
    payload = bytes([
        en_bit, pol_bit,
        raw_vol & 0xFF, (raw_vol >> 8) & 0xFF,
        delay_samples & 0xFF, (delay_samples >> 8) & 0xFF,
        0,                        # eq_mode (untested for now, leave 0)
        si & 0xFF,
    ])
    cmd = CMD_WRITE_CHANNEL_BASE + channel  # 0x1f00..0x1f07
    dsp.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)


def tone_phase_at(x: np.ndarray, sr: int, freq_hz: float) -> tuple[float, float]:
    """Return (level_dbfs, phase_radians) of the tone at freq_hz in x.

    Uses a single complex DFT bin at the target frequency for clean
    phase measurement.
    """
    n = len(x)
    t = np.arange(n) / sr
    # Complex DFT bin via inner product (no windowing — would distort phase)
    ref = np.exp(-2j * np.pi * freq_hz * t)
    bin_val = np.sum(x * ref) / n  # complex, normalised
    mag = abs(bin_val) * 2.0       # *2 for one-sided spectrum
    phase = math.atan2(bin_val.imag, bin_val.real)
    return (20 * math.log10(mag) if mag > 0 else float("-inf"), phase)


def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found")
    info = devs[0]
    print(f"DSP: {info['display_id']} path={info['path']!r}\n")

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        # Setup: master 0 dB, channel 2 audible at 0 dB, route IN1→OUT2
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_routing(DSP_OUT_INDEX,
                        in1=(DSP_IN_FOR_ROUTE == 1),
                        in2=(DSP_IN_FOR_ROUTE == 2),
                        in3=(DSP_IN_FOR_ROUTE == 3),
                        in4=(DSP_IN_FOR_ROUTE == 4))
        time.sleep(0.1)

        results = {}
        for polar in (False, True):
            # Use the high-level API now that polar is wired into Device.
            # (Earlier iterations called write_basic_record_with_polar() raw.)
            dsp.set_channel_polar(DSP_OUT_INDEX, polar=polar)
            time.sleep(0.1)  # let device settle
            cap = play_one_scarlett_out(SCARLETT_OUT_IDX)
            n_lead = int(0.05 * cap.sr)
            n_tail = int(0.05 * cap.sr)
            body = slice(n_lead, len(cap) - n_tail)
            in1_db, in1_phase = tone_phase_at(cap.in1[body], cap.sr, TONE_HZ)
            lp1_db, lp1_phase = tone_phase_at(cap.lp1[body], cap.sr, TONE_HZ)
            phase_diff = (in1_phase - lp1_phase + math.pi) % (2 * math.pi) - math.pi
            results[polar] = {
                "in1_db": in1_db, "in1_phase": in1_phase,
                "lp1_db": lp1_db, "lp1_phase": lp1_phase,
                "phase_diff_rad": phase_diff,
                "phase_diff_deg": math.degrees(phase_diff),
            }
            print(f"polar={polar}:")
            print(f"  reference (LP1): {lp1_db:+6.1f} dBFS, phase {math.degrees(lp1_phase):+7.1f}°")
            print(f"  through DSP (IN1): {in1_db:+6.1f} dBFS, phase {math.degrees(in1_phase):+7.1f}°")
            print(f"  Δphase (IN1 − LP1, mod 360):  {math.degrees(phase_diff):+7.1f}°")
            print()

        # Restore polar=False, cleanup
        dsp.set_channel_polar(DSP_OUT_INDEX, polar=False)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)

    # Diagnose
    print("=== Verdict ===")
    diff_normal = results[False]["phase_diff_deg"]
    diff_inverted = results[True]["phase_diff_deg"]
    flip = (diff_inverted - diff_normal + 540) % 360 - 180  # signed
    print(f"  Δphase normal:    {diff_normal:+7.1f}°")
    print(f"  Δphase inverted:  {diff_inverted:+7.1f}°")
    print(f"  Difference:       {flip:+7.1f}°  (expected ±180° if polar works)")
    if abs(abs(flip) - 180) < 30:
        print("  ✓ POLAR FLIPS PHASE BY 180°. Phase invert byte[1] confirmed.")
    elif abs(flip) < 30:
        print("  ✗ Polar didn't change phase. byte[1] probably isn't the polar bit,")
        print("    OR write was rejected, OR the DSP ignores polar in this state.")
    else:
        print("  ≈ Inconclusive — phase shifted by something between 0 and 180°.")
        print("    Could be DSP-side delay drift between captures. Try re-running.")


if __name__ == "__main__":
    main()
