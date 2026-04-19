"""Live Scarlett input level meter — for tuning input gain knobs while
playing a continuous tone through the DSP-408.

Plays a 1 kHz tone forever on Scarlett OUT 1+2, with DSP routing
IN1→OUT1 + IN2→OUT2 enabled, and prints the level reaching each
Scarlett input every 100 ms. Adjust the front-panel knobs until you
see a healthy level (e.g. -20 to -10 dBFS — well above the -90 dBFS
noise floor).

Stop with Ctrl-C.

    sg audio -c '.venv/bin/python tests/loopback/live_meter.py'
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/mbelleau/dsp408")

import numpy as np
import sounddevice as sd
from audio_io import (
    CAPTURE_CHANNELS,
    DEFAULT_SR,
    PLAYBACK_CHANNELS,
    find_scarlett,
    float_to_int32,
    int32_to_float,
    rms_dbfs,
)

from dsp408 import Device, enumerate_devices


def bar(db: float, width: int = 32) -> str:
    """Draw a quick ASCII level bar from -90 dBFS (left) to 0 dBFS (right)."""
    db = max(-90.0, min(0.0, db))
    pct = (db + 90) / 90.0
    n = int(pct * width)
    return "[" + "#" * n + " " * (width - n) + "]"


def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found")
    info = devs[0]
    print(f"DSP: {info['display_id']}  path={info['path']!r}")
    print("Setting up: master 0 dB, ch1+ch2 audible at 0 dB, route IN1→OUT1, IN2→OUT2")

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
        # Mirror routing: IN1→OUT1, IN2→OUT2
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_routing(0, in1=True, in2=False, in3=False, in4=False)
        dsp.set_routing(1, in1=False, in2=True, in3=False, in4=False)

        # Set up continuous playback of 1 kHz on both outputs
        dev_idx = find_scarlett()
        sr = DEFAULT_SR
        block = 4800   # 100 ms blocks
        phase = [0.0]
        freq = 1000.0
        amp = 10 ** (-20.0 / 20.0)  # -20 dBFS

        # Capture buffer — refilled async by the input callback
        latest_in = np.zeros((block, CAPTURE_CHANNELS), dtype=np.float64)

        def out_cb(outdata, frames, time_info, status):
            t = (np.arange(frames) + phase[0]) / sr
            wav = amp * np.sin(2 * np.pi * freq * t)
            phase[0] += frames
            stereo = np.zeros((frames, PLAYBACK_CHANNELS), dtype=np.float64)
            stereo[:, 0] = wav
            stereo[:, 1] = wav
            outdata[:] = float_to_int32(stereo)

        def in_cb(indata, frames, time_info, status):
            nonlocal latest_in
            latest_in = int32_to_float(indata)

        with sd.OutputStream(
            samplerate=sr, blocksize=block, channels=PLAYBACK_CHANNELS,
            dtype="int32", device=dev_idx, callback=out_cb,
        ), sd.InputStream(
            samplerate=sr, blocksize=block, channels=CAPTURE_CHANNELS,
            dtype="int32", device=dev_idx, callback=in_cb,
        ):
            print()
            print("Live levels — turn the Scarlett front-panel knobs and watch IN1+IN2 rise.")
            print("(Ctrl-C to stop. Healthy line-in level is roughly -20 to -10 dBFS.)")
            print()
            try:
                while True:
                    in1_db = rms_dbfs(latest_in[:, 0])
                    in2_db = rms_dbfs(latest_in[:, 1])
                    lp1_db = rms_dbfs(latest_in[:, 2])
                    lp2_db = rms_dbfs(latest_in[:, 3])
                    print(f"  IN1 {in1_db:+6.1f} dBFS {bar(in1_db)}  "
                          f"IN2 {in2_db:+6.1f} dBFS {bar(in2_db)}  "
                          f"(LP {lp1_db:+5.1f} / {lp2_db:+5.1f})",
                          end="\r", flush=True)
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print()
                print("done.")
                # Cleanup routing
                for ch in range(8):
                    dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)


if __name__ == "__main__":
    main()
