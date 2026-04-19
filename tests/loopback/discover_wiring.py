"""Probe the DSP-408 ↔ Scarlett wiring map empirically.

Drives one Scarlett output at a time with a known tone, routes one DSP
input to one DSP output, and watches which Scarlett input picks it up.
After scanning every (Scarlett out × DSP in × DSP out) combination we
have the complete signal-flow mapping and can tell the user exactly
which cable goes where.

Run after wiring (any wiring — that's the point):

    sg audio -c '.venv/bin/python tests/loopback/discover_wiring.py'

Output:
    A truth table of "Scarlett OUT N + DSP route IN_a→OUT_b" → "level seen
    on Scarlett IN 1, IN 2 (and the device-loopback reference)", followed
    by the inferred cable map.

The signal level threshold for "this got there" is -50 dBFS — well above
the typical -90 dBFS noise floor we measured with nothing plugged in.
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

# Threshold for "this signal arrived" (well above ambient noise floor)
ARRIVED_DBFS = -50.0
TONE_HZ = 1000.0
TONE_AMP_DBFS = -20.0
TONE_DUR_S = 0.4

# DSP-408 has 4 inputs, 8 outputs. We scan all 4 inputs and the first 4
# outputs (the user's second cable set could be on any of OUT 1..4).
DSP_INS = (1, 2, 3, 4)
DSP_OUTS = (0, 1, 2, 3)  # output index 0..7 per dsp408 API


def play_one_scarlett_out(out_idx: int, freq_hz: float = TONE_HZ,
                           amp_dbfs: float = TONE_AMP_DBFS,
                           duration_s: float = TONE_DUR_S):
    """Generate a 2-channel block where only `out_idx` carries the tone."""
    tone = sine(freq_hz, duration_s, amp_dbfs=amp_dbfs)
    block = np.zeros((len(tone), PLAYBACK_CHANNELS), dtype=np.float64)
    block[:, out_idx] = tone
    return play_and_record(block, sr=DEFAULT_SR)


def measure_arrival(cap, freq_hz: float) -> dict:
    """Return tone level (dBFS) per Scarlett capture channel."""
    n_lead = int(0.05 * cap.sr)
    n_tail = int(0.05 * cap.sr)
    body = slice(n_lead, len(cap) - n_tail)
    return {
        "in1": tone_level_at(cap.in1[body], cap.sr, freq_hz),
        "in2": tone_level_at(cap.in2[body], cap.sr, freq_hz),
        "lp1": tone_level_at(cap.lp1[body], cap.sr, freq_hz),
        "lp2": tone_level_at(cap.lp2[body], cap.sr, freq_hz),
    }


def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found")
    info = devs[0]
    print(f"DSP: {info['display_id']} path={info['path']!r}\n")

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        # Master at unity, all channels audible at 0 dB, no delay
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
        # All routing OFF as baseline
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        time.sleep(0.1)

        # ── Sanity: with no routing, Scarlett INs should be silent. Also
        # verify both Scarlett outputs ARE physically producing sound by
        # checking the device-loopback channels.
        print("=== Sanity checks ===")
        for sc_out in (0, 1):
            cap = play_one_scarlett_out(sc_out)
            m = measure_arrival(cap, TONE_HZ)
            ok_silent = max(m["in1"], m["in2"]) < ARRIVED_DBFS
            ok_lp = m[f"lp{sc_out+1}"] > -40  # loopback should see what we played
            status_lp = "✓" if ok_lp else f"✗ (Scarlett OUT {sc_out+1} producing no sound!)"
            status_silent = "✓ silent (good)" if ok_silent else "✗ (signal on IN with no routing — bleed?)"
            print(f"  Scarlett OUT {sc_out+1} → "
                  f"LP{sc_out+1}={m[f'lp{sc_out+1}']:+6.1f} dBFS {status_lp}, "
                  f"IN1={m['in1']:+6.1f} dBFS, IN2={m['in2']:+6.1f} dBFS  {status_silent}")
        print()

        # ── Probe full matrix ──
        results = []
        for scarlett_out in (0, 1):
            print(f"=== Scarlett OUT {scarlett_out+1} test (driving 1 kHz at -20 dBFS, OUT {2-scarlett_out} silent) ===")
            for dsp_out in DSP_OUTS:
                for dsp_in in DSP_INS:
                    # All routing OFF first
                    for ch in range(8):
                        dsp.set_routing(ch, in1=False, in2=False,
                                        in3=False, in4=False)
                    # Then enable JUST one
                    dsp.set_routing(dsp_out,
                                    in1=(dsp_in == 1),
                                    in2=(dsp_in == 2),
                                    in3=(dsp_in == 3),
                                    in4=(dsp_in == 4))
                    time.sleep(0.05)
                    cap = play_one_scarlett_out(scarlett_out)
                    m = measure_arrival(cap, TONE_HZ)
                    results.append((scarlett_out, dsp_in, dsp_out, m))
                    arrived = []
                    if m["in1"] > ARRIVED_DBFS:
                        arrived.append(f"IN1({m['in1']:+5.1f})")
                    if m["in2"] > ARRIVED_DBFS:
                        arrived.append(f"IN2({m['in2']:+5.1f})")
                    arrived_str = ", ".join(arrived) if arrived else "—"
                    if arrived:
                        print(f"  route IN{dsp_in}→OUT{dsp_out+1}: "
                              f"arrived on Scarlett {arrived_str}")
            print()

        # Cleanup
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)

    # ── Infer the mapping ──
    print("\n=== Inferred cable map ===")
    # For each Scarlett output, find which DSP input(s) it physically reaches.
    # A DSP input is reached if a routing test of that input arrives anywhere.
    scarlett_to_dsp_in: dict[int, list[int]] = {0: [], 1: []}
    for sc_out, dsp_in, dsp_out, m in results:
        if max(m["in1"], m["in2"]) > ARRIVED_DBFS:
            if dsp_in not in scarlett_to_dsp_in[sc_out]:
                scarlett_to_dsp_in[sc_out].append(dsp_in)

    # For each DSP output, find which Scarlett input(s) it physically reaches.
    dsp_out_to_scarlett_in: dict[int, list[int]] = {o: [] for o in DSP_OUTS}
    for sc_out, dsp_in, dsp_out, m in results:
        if m["in1"] > ARRIVED_DBFS and 1 not in dsp_out_to_scarlett_in[dsp_out]:
            dsp_out_to_scarlett_in[dsp_out].append(1)
        if m["in2"] > ARRIVED_DBFS and 2 not in dsp_out_to_scarlett_in[dsp_out]:
            dsp_out_to_scarlett_in[dsp_out].append(2)

    print("\nInput cables (Scarlett OUT → DSP IN):")
    for sc_out, dsp_ins in scarlett_to_dsp_in.items():
        if dsp_ins:
            ins_str = ', '.join(str(d) for d in sorted(dsp_ins))
            extra = "  ⚠ Y CABLE? signal split to multiple inputs" if len(dsp_ins) > 1 else ""
            print(f"  Scarlett OUT {sc_out+1}  →  DSP IN {ins_str}{extra}")
        else:
            print(f"  Scarlett OUT {sc_out+1}  →  (no DSP input received signal)")

    print("\nOutput cables (DSP OUT → Scarlett IN):")
    for dsp_out in DSP_OUTS:
        sc_ins = dsp_out_to_scarlett_in[dsp_out]
        if sc_ins:
            ins_str = ', '.join(str(s) for s in sorted(sc_ins))
            extra = "  ⚠ Y CABLE? signal split to multiple inputs" if len(sc_ins) > 1 else ""
            print(f"  DSP OUT {dsp_out+1}      →  Scarlett IN {ins_str}{extra}")

    print()
    print("(DSP outputs not listed above = no Scarlett input received their signal,")
    print(" i.e. those DSP outputs aren't wired back to the Scarlett.)")


if __name__ == "__main__":
    main()
