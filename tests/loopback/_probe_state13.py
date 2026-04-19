"""Decode the 10 bytes returned by cmd=0x13 (state_13).

Currently exposed in MQTT as a raw hex blob; we want to break it into
per-channel meters. Hypothesis: 4 bytes for the 4 inputs + some bytes
for outputs (or master / total / something).

Empirical method:
  * Drive a tone at varying amplitudes through each Scarlett OUT (which
    feeds DSP IN 1 only — IN 2..4 are unwired but should still respond
    to internal signal if any leaks; otherwise stay at the noise floor).
  * Snapshot state_13 at each level, see which BYTE moves with which
    channel and HOW it moves (dB curve fit).
  * Mute master and observe — does anything change? (tells us if any
    bytes are output meters vs input meters.)
"""
import sys, time
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
from audio_io import DEFAULT_SR, mono_to_stereo, play_and_record, sine
from dsp408 import Device, enumerate_devices

SR = DEFAULT_SR
DSP_IN_INDEX = 0   # Scarlett OUT 1 → DSP IN 1
DSP_OUT_INDEX = 1  # DSP OUT 2 → Scarlett IN 1


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


def play_tone_async(freq, dur, amp_dbfs, side="left"):
    """Play a tone but don't wait. Returns the audio_io capture."""
    tone = sine(freq, dur, amp_dbfs=amp_dbfs)
    return play_and_record(mono_to_stereo(tone, left=(side == "left"),
                                           right=(side == "right")))


def snapshot_during_tone(dsp, freq=1000, dur=2.0, amp_dbfs=-20.0,
                        n_samples=10, side="left"):
    """Play `dur` seconds of tone; sample state_13 `n_samples` times during it.
    Returns list of bytes blobs."""
    import threading
    samples = []
    stop = threading.Event()

    def poll_loop():
        while not stop.is_set():
            try:
                samples.append(bytes(dsp.read_state_0x13()))
            except Exception as e:
                samples.append(f"err:{e}".encode())
            time.sleep(dur / max(n_samples, 1) * 0.6)

    poll_t = threading.Thread(target=poll_loop, daemon=True)
    poll_t.start()
    play_tone_async(freq, dur, amp_dbfs, side)
    stop.set(); poll_t.join(timeout=1.0)
    return samples


info = enumerate_devices()[0]
with Device.open(path=info["path"]) as dsp:
    dsp.connect()
    setup(dsp)

    # Baseline: no signal
    print("=== Baseline state_13 (no input) ===")
    base_samples = []
    for _ in range(5):
        base_samples.append(bytes(dsp.read_state_0x13()))
        time.sleep(0.1)
    for s in base_samples:
        print(f"  {s.hex()}  =  {' '.join(f'{b:>3d}' for b in s)}")
    base = base_samples[-1]

    # ── Test 1: drive Scarlett OUT 1 at varying levels ──
    print("\n=== Drive Scarlett OUT 1 (→ DSP IN 1) at varying dBFS ===")
    print("Expecting one BYTE to move proportionally with input level.")
    print(f"{'dBFS':>6}  state_13 (bytes)                     | diff vs base")
    print("-" * 90)
    for dbfs in (-60, -40, -30, -20, -10, -3, 0):
        samples = snapshot_during_tone(dsp, freq=1000, dur=1.5,
                                        amp_dbfs=dbfs, n_samples=8,
                                        side="left")
        # Take the median of each byte across samples (robust to startup glitch)
        if not samples or isinstance(samples[0], bytes) is False:
            continue
        arr = np.array([list(s[:10]) for s in samples if len(s) >= 10])
        med = np.median(arr, axis=0).astype(int)
        diff = med - np.array(list(base[:10]))
        med_str = " ".join(f"{int(b):>3d}" for b in med)
        diff_str = " ".join(f"{d:+4d}" for d in diff)
        print(f"  {dbfs:>4} | {med_str}  |  {diff_str}")

    # ── Test 2: drive Scarlett OUT 2 (→ DSP IN 2) ──
    print("\n=== Drive Scarlett OUT 2 (→ DSP IN 2) at varying dBFS ===")
    print(f"{'dBFS':>6}  state_13 bytes                        | diff vs base")
    print("-" * 90)
    for dbfs in (-60, -20, 0):
        samples = snapshot_during_tone(dsp, freq=1000, dur=1.5,
                                        amp_dbfs=dbfs, n_samples=8,
                                        side="right")
        arr = np.array([list(s[:10]) for s in samples if len(s) >= 10])
        if not arr.size: continue
        med = np.median(arr, axis=0).astype(int)
        diff = med - np.array(list(base[:10]))
        print(f"  {dbfs:>4} | {' '.join(f'{int(b):>3d}' for b in med)}  "
              f"|  {' '.join(f'{d:+4d}' for d in diff)}")

    # ── Test 3: routing change (does muting OUT 2 change any byte?) ──
    print("\n=== Mute DSP OUT 2 (was the only routed output) ===")
    dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=True); time.sleep(0.3)
    samples = snapshot_during_tone(dsp, freq=1000, dur=1.5, amp_dbfs=-10,
                                    n_samples=8, side="left")
    arr = np.array([list(s[:10]) for s in samples if len(s) >= 10])
    med = np.median(arr, axis=0).astype(int)
    diff = med - np.array(list(base[:10]))
    print(f"  -10 dBFS in IN1, OUT2 muted | {' '.join(f'{int(b):>3d}' for b in med)}"
          f"  |  diff: {' '.join(f'{d:+4d}' for d in diff)}")

    # Restore
    dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False)
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)
