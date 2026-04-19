"""Re-test slope=8 with the CORRECT capture channel (cap.in1, where DSP
OUT 2 actually lands).

The original conclusion "slope=8 mutes the channel" was drawn from a run
that was reading cap.in2 by mistake — the unwired input. So it was noise
floor regardless of slope.  This script re-runs the same test on the
correct capture channel and compares slope=8 against:
  - slope=1 (the firmware default, 12 dB/oct)
  - a wide-open Butterworth (HPF 10 Hz / LPF 22 kHz / slope=0)
"""
import sys, time
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
from audio_io import (
    DEFAULT_SR, mono_to_stereo, play_and_record, sine, tone_level_at,
)
from dsp408 import Device, enumerate_devices

SR = DEFAULT_SR
DSP_OUT_INDEX = 1     # OUT 2 (the only output wired back)
TONE_DUR_S = 0.20
TONE_AMP_DBFS = -20.0

# Probe a handful of frequencies spanning the audio band
FREQS = [50, 200, 500, 1000, 2000, 5000, 10000]


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
    time.sleep(0.2)


def measure(freq):
    tone = sine(freq, TONE_DUR_S, amp_dbfs=TONE_AMP_DBFS)
    cap = play_and_record(mono_to_stereo(tone, left=True, right=False))
    n_lead = int(0.06 * SR); n_tail = int(0.04 * SR)
    body = slice(n_lead, len(cap) - n_tail)
    bw = max(8.0, freq * 0.02)
    ref = tone_level_at(cap.lp1[body], cap.sr, freq, bw_hz=bw)
    sig_in1 = tone_level_at(cap.in1[body], cap.sr, freq, bw_hz=bw)
    return ref, sig_in1


CONFIGS = [
    ("wide-open BW   ", 10, 0, 0, 22000, 0, 0),
    ("default 12dB/oct", 20, 0, 1, 20000, 0, 1),
    ("HPF slope=8     ", 200, 0, 8, 20000, 0, 1),
    ("LPF slope=8     ", 20,  0, 1, 5000,  0, 8),
    ("BOTH slope=8    ", 200, 0, 8, 5000,  0, 8),
    ("HPF freq=20 slp8", 20,  0, 8, 20000, 0, 1),
]


info = enumerate_devices()[0]
with Device.open(path=info["path"]) as dsp:
    dsp.connect()
    setup(dsp)

    print(f"{'config':<18s} {'  '.join(f'{f:>7}Hz' for f in FREQS)}")
    print("-" * (18 + len(FREQS) * 11))

    for name, hf, hflt, hslp, lf, lflt, lslp in CONFIGS:
        dsp.set_crossover(DSP_OUT_INDEX, hf, hflt, hslp, lf, lflt, lslp)
        time.sleep(0.4)
        cells = []
        for f in FREQS:
            ref, sig = measure(f)
            cells.append(f"{sig - ref:+7.1f}")
        print(f"{name:<18s} {'  '.join(cells)} dB rel ref")

    # restore
    dsp.set_crossover(DSP_OUT_INDEX, 20, 0, 1, 20000, 0, 1)
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)
