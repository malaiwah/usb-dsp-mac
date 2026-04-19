"""DSP-408 passthrough test — validates the FULL rig once cables are wired.

Plays a 1 kHz tone via Scarlett OUT, sends it through the DSP-408
(routing IN1 → OUT1 with default settings), and measures the round-trip
level on Scarlett IN.

Run after wiring:
    Scarlett OUT 1 → DSP-408 IN 1
    Scarlett OUT 2 → DSP-408 IN 2
    DSP-408 OUT 1  → Scarlett IN 1
    DSP-408 OUT 2  → Scarlett IN 2

Usage:
    sg audio -c '.venv/bin/python tests/loopback/test_dsp_passthrough.py'

Expected behaviour:
- Scarlett loopback channels (lp1/lp2) read what we just played (~ -20 dBFS).
- Scarlett analog inputs (in1/in2) read whatever passes through the DSP.
  With default routing (no inputs routed to any output) they should be silent.
- After we explicitly route IN1 → OUT1, in1 should match the played tone
  (modulo small DSP-induced gain/phase shift).
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/mbelleau/dsp408")

from dsp408 import Device, enumerate_devices

from audio_io import (
    log_sweep,
    mono_to_stereo,
    peak_dbfs,
    play_and_record,
    rms_dbfs,
    sine,
    tone_level_at,
)


def report(label: str, cap, freq_hz: float | None = None) -> None:
    n_lead = int(0.05 * cap.sr)
    n_tail = int(0.05 * cap.sr)
    body = slice(n_lead, len(cap) - n_tail)
    print(f"\n--- {label} ---")
    for name, sig in [("IN 1     (DSP OUT 1 → Scarlett IN 1)", cap.in1[body]),
                      ("IN 2     (DSP OUT 2 → Scarlett IN 2)", cap.in2[body]),
                      ("loopback OUT 1 (what we played)     ", cap.lp1[body]),
                      ("loopback OUT 2 (what we played)     ", cap.lp2[body])]:
        line = (f"  {name}: rms={rms_dbfs(sig):+7.1f} dBFS  "
                f"peak={peak_dbfs(sig):+7.1f} dBFS")
        if freq_hz is not None:
            line += f"  tone@{int(freq_hz)}Hz={tone_level_at(sig, cap.sr, freq_hz):+7.1f} dBFS"
        print(line)


def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found on the bus")
    info = devs[0]
    print(f"DSP: {info['display_id']} path={info['path']!r}")

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()

        # Make sure master volume is at a reasonable level (not muted, not max)
        dsp.set_master(db=0.0, muted=False)
        # Turn ON channel 0 audible at unity gain
        dsp.set_channel(0, db=0.0, muted=False, delay_samples=0)
        # And channel 1 likewise
        dsp.set_channel(1, db=0.0, muted=False, delay_samples=0)

        # Step 1 — baseline: route NOTHING, expect silence on Scarlett IN 1+2
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        time.sleep(0.05)
        cap = play_and_record(mono_to_stereo(sine(1000, 0.5, amp_dbfs=-20)))
        report("Baseline: no routing — DSP outputs should be silent",
               cap, freq_hz=1000)

        # Step 2 — route IN1 → OUT1 at full level, expect tone on Scarlett IN 1
        dsp.set_routing(0, in1=True, in2=False, in3=False, in4=False)
        # Also route IN2 → OUT2 for stereo
        dsp.set_routing(1, in1=False, in2=True, in3=False, in4=False)
        time.sleep(0.05)
        cap = play_and_record(mono_to_stereo(sine(1000, 0.5, amp_dbfs=-20)))
        report("Routed IN1→OUT1, IN2→OUT2 — should hear tone on IN 1+2",
               cap, freq_hz=1000)

        # Step 3 — drop channel volume by 12 dB, measure
        dsp.set_channel_volume(0, db=-12)
        time.sleep(0.05)
        cap = play_and_record(mono_to_stereo(sine(1000, 0.5, amp_dbfs=-20)))
        report("Channel 0 volume = -12 dB — IN 1 should drop ~12 dB",
               cap, freq_hz=1000)
        dsp.set_channel_volume(0, db=0)

        # Step 4 — frequency response sweep on channel 0 with default crossover
        # (HPF=20Hz LPF=20kHz BW/12dB) — should be roughly flat from 30 Hz–18 kHz.
        # Just plot first/last/middle bins for now.
        sweep = log_sweep(20, 20000, 2.0, amp_dbfs=-20)
        cap = play_and_record(mono_to_stereo(sweep))
        # Just verify capture succeeded; analysis comes later.
        body = slice(int(0.05 * cap.sr), len(cap) - int(0.05 * cap.sr))
        print(f"\n--- Sweep 20Hz→20kHz on IN 1 ---")
        print(f"  IN 1 captured: rms={rms_dbfs(cap.in1[body]):+7.1f} dBFS  "
              f"peak={peak_dbfs(cap.in1[body]):+7.1f} dBFS  "
              f"({len(cap)} samples = {len(cap)/cap.sr:.2f} s)")

        # Cleanup — leave routing OFF so we don't surprise the next session
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)


if __name__ == "__main__":
    main()
