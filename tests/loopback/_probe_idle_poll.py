"""Probe cmd=0x03 (idle_poll / streaming response) for live meter data.

Capture analysis showed cmd=0x03 returns 15 bytes during streaming, ending
in 0x01. With no audio active, leading 14 bytes were all zero. Hypothesis:
those 14 bytes are per-channel meter levels.

Layout guesses for 14 bytes:
  - 4 inputs + 8 outputs + 2 master = 14   (most likely)
  - 8 outputs + 6 inputs/aux                = 14
  - other?

Method: drive Scarlett OUT 1 (→ DSP IN 1) at varying levels, snapshot
idle_poll during each tone. Identify the moving byte(s) and the encoding.
"""
import sys, time, threading
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
from audio_io import DEFAULT_SR, mono_to_stereo, play_and_record, sine
from dsp408 import Device, enumerate_devices

SR = DEFAULT_SR
DSP_OUT_INDEX = 1


def setup(dsp):
    dsp.set_master(db=0.0, muted=False)
    for _ in range(8):
        dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False); time.sleep(0.05)
        dsp.read_channel_state(DSP_OUT_INDEX)
    for ch in range(8):
        if ch != DSP_OUT_INDEX:
            dsp.set_channel(ch, db=0.0, muted=True)
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
    dsp.set_routing(DSP_OUT_INDEX, in1=True, in2=False, in3=False, in4=False)
    dsp.set_crossover(DSP_OUT_INDEX, 10, 0, 0, 22000, 0, 0)
    time.sleep(0.3)


def play_async(freq, dur, dbfs, side="left"):
    tone = sine(freq, dur, amp_dbfs=dbfs)
    return play_and_record(mono_to_stereo(tone,
                                          left=(side == "left"),
                                          right=(side == "right")))


def snapshot_during_tone(dsp, freq, dur, dbfs, n=20, side="left"):
    samples = []
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try: samples.append(bytes(dsp.idle_poll()))
            except Exception as e: samples.append(repr(e).encode())
            time.sleep(dur / max(n, 1) * 0.6)
    t = threading.Thread(target=loop, daemon=True); t.start()
    play_async(freq, dur, dbfs, side)
    stop.set(); t.join(timeout=1.0)
    return samples


def fmt(b):
    return " ".join(f"{x:>3d}" for x in b[:15])


info = enumerate_devices()[0]
with Device.open(path=info["path"]) as dsp:
    dsp.connect()
    setup(dsp)

    print("=== Baseline idle_poll (no input) ===")
    base = []
    for _ in range(8):
        base.append(bytes(dsp.idle_poll())); time.sleep(0.1)
    for s in base:
        print(f"  len={len(s)}: {fmt(s)}")

    bref = base[-1]

    def show(label, samples):
        arr = np.array([list(s[:15]) for s in samples if len(s) >= 15])
        if not arr.size: print(f"  {label}: NO DATA"); return None, None
        med = np.median(arr, axis=0).astype(int)
        peak = arr.max(axis=0).astype(int)
        diff = peak - np.array(list(bref[:15]))
        print(f"  {label}")
        print(f"    median: {fmt(med)}")
        print(f"    peak:   {fmt(peak)}")
        print(f"    Δpeak:  {' '.join(f'{d:+4d}' for d in diff)}")
        return med, peak

    print("\n=== Drive Scarlett OUT 1 (→ DSP IN 1) ===")
    for dbfs in (-60, -40, -30, -20, -10, -3, 0):
        s = snapshot_during_tone(dsp, 1000, 1.5, dbfs, n=25, side="left")
        show(f"DSP IN 1 @ {dbfs:>+4} dBFS", s)

    print("\n=== Drive Scarlett OUT 2 (→ DSP IN 2) ===")
    for dbfs in (-60, -20, -3):
        s = snapshot_during_tone(dsp, 1000, 1.5, dbfs, n=25, side="right")
        show(f"DSP IN 2 @ {dbfs:>+4} dBFS", s)

    # Output meter check: drive in1, route to OUT 2 (already routed)
    print("\n=== Drive in1, observe with OUT 2 routed (=output should peak) ===")
    s = snapshot_during_tone(dsp, 1000, 1.5, -10, n=25, side="left")
    show("IN1=-10 dBFS, OUT2 routed", s)

    print("\n=== Same, OUT 2 muted (output should drop) ===")
    dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=True); time.sleep(0.3)
    s = snapshot_during_tone(dsp, 1000, 1.5, -10, n=25, side="left")
    show("IN1=-10 dBFS, OUT2 MUTED", s)
    dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False); time.sleep(0.2)

    # Master mute: tells us if any byte is post-master
    print("\n=== Master MUTED, tone on IN1 ===")
    dsp.set_master(db=0.0, muted=True); time.sleep(0.3)
    s = snapshot_during_tone(dsp, 1000, 1.5, -10, n=25, side="left")
    show("IN1=-10 dBFS, master MUTED", s)
    dsp.set_master(db=0.0, muted=False)

    # Restore
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)
