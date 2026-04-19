"""DSP-408 master volume calibration via Scarlett loopback.

The master volume control sits at the master output bus (after per-channel
processing). We expect the encoding ``raw = dB + 60`` (range 0..66 = -60..+6 dB
per protocol.py).

Setup (mono): same as test_gain_calibration.py — Scarlett OUT 1 → DSP IN
→ DSP routes to OUT 2 → Scarlett IN 1.

Method:
  1. Hold channel 2 volume at 0 dB.
  2. Sweep MASTER from -60 dB to +6 dB in 6 dB steps.
  3. Measure delta vs reference at master = 0 dB.

If the encoding is correct, measured drop should equal requested drop
within ~0.5 dB across the safe range.

Run:
    sg audio -c '.venv/bin/python tests/loopback/test_master_calibration.py'
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/mbelleau/dsp408")

import numpy as np

from dsp408 import Device, enumerate_devices

from audio_io import (
    DEFAULT_SR,
    PLAYBACK_CHANNELS,
    play_and_record,
    sine,
    tone_level_at,
)

TONE_HZ = 1000.0
TONE_AMP_DBFS = -20.0
TONE_DUR_S = 0.4
SCARLETT_OUT_IDX = 0
DSP_OUT_INDEX = 1     # OUT 2 (the only output wired back)
DSP_IN_FOR_ROUTE = 1  # IN 1

# Master spans -60..+6 dB. Step 6 dB.
SWEEP_DB = [+6, +3, 0, -3, -6, -12, -18, -24, -30, -40, -50, -60]


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


def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found")
    info = devs[0]
    print(f"DSP: {info['display_id']} path={info['path']!r}")
    print(f"Master sweep: {SWEEP_DB}\n")

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        # Channel 2 unmuted at 0 dB (volume held constant)
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
        # Route IN 1 → OUT 2 only
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_routing(DSP_OUT_INDEX,
                        in1=(DSP_IN_FOR_ROUTE == 1),
                        in2=(DSP_IN_FOR_ROUTE == 2),
                        in3=(DSP_IN_FOR_ROUTE == 3),
                        in4=(DSP_IN_FOR_ROUTE == 4))
        time.sleep(0.1)

        # Reference at master = 0 dB
        dsp.set_master(db=0.0, muted=False)
        time.sleep(0.05)
        cap = play_one_scarlett_out(SCARLETT_OUT_IDX)
        ref_dbfs = measure(cap)
        if ref_dbfs < -60:
            sys.exit(f"!! reference {ref_dbfs:+.1f} dBFS too low — "
                     "check signal flow")
        print(f"Reference (master @  +0.0 dB): {ref_dbfs:+6.1f} dBFS\n")
        print(f"  {'requested':>10s} {'measured':>10s} {'delta':>9s} "
              f"{'expected':>10s} {'error':>8s} {'verdict':>8s}")
        print(f"  {'─'*10} {'─'*10} {'─'*9} {'─'*10} {'─'*8} {'─'*8}")

        results = []
        for req_db in SWEEP_DB:
            dsp.set_master(db=float(req_db), muted=False)
            time.sleep(0.05)
            cap = play_one_scarlett_out(SCARLETT_OUT_IDX)
            meas = measure(cap)
            delta = meas - ref_dbfs
            err = delta - req_db
            verdict = "✓" if abs(err) < 1.0 else "≈" if abs(err) < 3.0 else "✗"
            results.append((req_db, meas, delta, err, verdict))
            print(f"  {req_db:+10.1f} {meas:+10.1f} {delta:+9.1f} "
                  f"{req_db:+10.1f} {err:+8.1f} {verdict:>8s}")

        # Restore master to 0 dB and route off
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)

    print()
    errs_in_range = [r[3] for r in results if -40 <= r[0] <= 6]
    if errs_in_range:
        max_err = max(abs(e) for e in errs_in_range)
        print(f"Summary (req=-40..+6 dB): max error = {max_err:.2f} dB")
        if max_err < 1.0:
            print("  ✓ Master encoding (raw = dB + 60) confirmed within ±1 dB.")
        elif max_err < 3.0:
            print("  ≈ Encoding mostly correct but with measurable error.")
        else:
            print("  ✗ Encoding mismatched.")


if __name__ == "__main__":
    main()
