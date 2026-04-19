"""DSP-408 channel volume calibration via Scarlett loopback.

Validates that our gain encoding ``raw = (dB × 10) + 600`` matches the
device's actual attenuation behaviour.

Setup (Option A — single-channel mono test):

    Scarlett OUT 1 → (any DSP IN, via existing wiring) → DSP routes to OUT 2 → Scarlett IN 1

We hold the input source fixed (Scarlett OUT 1 at -20 dBFS @ 1 kHz, DSP
routing IN1 → OUT 2), then sweep the OUT-2 channel-volume from 0 down to
-60 dB in 6 dB steps. We measure the Scarlett IN 1 level at each step.

If our encoding is correct, the measured-vs-requested dB delta should
follow a y=x line within ~0.5 dB across the useful range.

Run:
    sg audio -c '.venv/bin/python tests/loopback/test_gain_calibration.py'
"""
from __future__ import annotations

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

TONE_HZ = 1000.0
TONE_AMP_DBFS = -20.0
TONE_DUR_S = 0.4
SCARLETT_OUT_IDX = 0  # OUT 1
DSP_OUT_INDEX = 1     # OUT 2 (the only output wired back)
DSP_IN_FOR_ROUTE = 1  # IN 1 — we route from this to OUT 2

# dB values to sweep on the channel volume control (0 dB is unity).
# Device range is -60..0 dB.
SWEEP_DB = [0, -3, -6, -9, -12, -18, -24, -30, -40, -50, -60]


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
    print(f"Sweeping channel {DSP_OUT_INDEX+1} volume across {len(SWEEP_DB)} steps")
    print(f"Source: Scarlett OUT {SCARLETT_OUT_IDX+1} (1 kHz @ {TONE_AMP_DBFS:+.0f} dBFS)")
    print(f"Sink:   Scarlett IN 1 (= DSP OUT {DSP_OUT_INDEX+1})")
    print()

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        # Hold master at unity, all channels unmuted, no delay
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
        # Route ONLY IN1 → OUT2 (the channel we'll vary)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_routing(DSP_OUT_INDEX,
                        in1=(DSP_IN_FOR_ROUTE == 1),
                        in2=(DSP_IN_FOR_ROUTE == 2),
                        in3=(DSP_IN_FOR_ROUTE == 3),
                        in4=(DSP_IN_FOR_ROUTE == 4))
        time.sleep(0.1)

        # Reference measurement at 0 dB
        dsp.set_channel_volume(DSP_OUT_INDEX, db=0.0)
        time.sleep(0.05)
        cap = play_one_scarlett_out(SCARLETT_OUT_IDX)
        ref_dbfs = measure(cap)
        if ref_dbfs < -60:
            sys.exit(f"!! reference reading {ref_dbfs:+.1f} dBFS is too low — "
                     "check DSP routing / cabling / Scarlett input gain knob")

        print(f"Reference (channel {DSP_OUT_INDEX+1} @  +0.0 dB): {ref_dbfs:+6.1f} dBFS measured\n")
        print(f"  {'requested':>10s} {'measured':>10s} {'delta meas-ref':>15s} "
              f"{'delta-vs-requested':>20s} {'verdict':>10s}")
        print(f"  {'─'*10} {'─'*10} {'─'*15} {'─'*20} {'─'*10}")

        results = []
        for req_db in SWEEP_DB:
            dsp.set_channel_volume(DSP_OUT_INDEX, db=float(req_db))
            time.sleep(0.05)
            cap = play_one_scarlett_out(SCARLETT_OUT_IDX)
            meas_dbfs = measure(cap)
            delta_meas = meas_dbfs - ref_dbfs
            err = delta_meas - req_db
            verdict = ("✓" if abs(err) < 1.0
                       else "≈" if abs(err) < 3.0
                       else "✗")
            results.append((req_db, meas_dbfs, delta_meas, err, verdict))
            print(f"  {req_db:+10.1f} {meas_dbfs:+10.1f} {delta_meas:+15.1f} "
                  f"{err:+20.1f} {verdict:>10s}")

        # Restore + cleanup
        dsp.set_channel_volume(DSP_OUT_INDEX, db=0.0)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)

    # Summary
    print()
    errs_in_range = [r[3] for r in results if r[0] >= -40]
    if errs_in_range:
        max_err = max(abs(e) for e in errs_in_range)
        print(f"Summary (req=-40..0 dB): max error = {max_err:.2f} dB")
        if max_err < 1.0:
            print("  ✓ Gain encoding (raw = (dB×10) + 600) confirmed within ±1 dB.")
        elif max_err < 3.0:
            print("  ≈ Encoding mostly correct but with measurable error.")
        else:
            print("  ✗ Encoding mismatched — investigate.")
    print()
    print("Note: for very low requested levels (-50, -60 dB) the measurement")
    print("approaches the noise floor; small errors there are expected.")


if __name__ == "__main__":
    main()
