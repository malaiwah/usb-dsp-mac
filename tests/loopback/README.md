# Loopback test rig — Scarlett 2i2 4th Gen + DSP-408

Empirical verification of DSP-408 control surfaces via audio loopback. The
Scarlett is the test instrument: we drive signals, record what comes back,
and compare to predict-vs-measured to validate every control we expose.

## Hardware setup

```
                           ┌──────────────┐
                           │  Mac / Linux │
                           └──┬────────┬──┘
                              │ USB    │ USB
                  Scarlett 2i2 4th Gen  DSP-408
                  ┌─────────────────┐   ┌─────────────────┐
                  │  OUT 1  IN 1    │   │  IN 1   OUT 1   │
                  │  OUT 2  IN 2    │   │  IN 2   OUT 2   │
                  └──┬───┬────┬──┬──┘   └──┬───┬────┬──┬──┘
                     │   │    │  │         │   │    │  │
                     │   └────┼──┼─────────┘   └────┘  │
                     │   ┌────┘  └──────────┐  ┌───────┘
                     │   │                  │  │
                     └───┼──────────────────┘  │
                         └─────────────────────┘
```

Cables (1/4" TRS ↔ RCA adapters):
- Scarlett OUT 1 (1/4" TRS) → DSP IN 1 (RCA)
- Scarlett OUT 2 (1/4" TRS) → DSP IN 2 (RCA)
- DSP OUT 1 (RCA)            → Scarlett IN 1 (1/4" combo XLR/TRS)
- DSP OUT 2 (RCA)            → Scarlett IN 2 (1/4" combo XLR/TRS)

## Why the Scarlett 4th Gen is ideal

It exposes **4 capture channels** in ALSA, not 2:
- `ch1` = analog IN 1 (= DSP OUT 1 in our rig)
- `ch2` = analog IN 2 (= DSP OUT 2)
- `ch3` = internal loopback of OUT 1 (what we just played)
- `ch4` = internal loopback of OUT 2

That means **we can sanity-check the rig before any cables are plugged in**
— `audio_io.selfloop_sanity()` plays a tone, reads back via ch3+4, and
verifies bit-perfect round-trip. If that works, the audio side is good and
any later issues are in the wiring or in the DSP.

## What's measurable

Single tone @ -20 dBFS, vary one DSP setting at a time, measure outputs:

| DSP setting | Test | What's validated |
|---|---|---|
| Channel volume -60..0 dB step 6 | Single 1 kHz tone in, measure output level | Gain encoding `(dB×10)+600` |
| Routing IN1→OUT1 = 0/25/50/75/100 | Same | **Whether the matrix is binary or u8 % (the open question)** |
| Phase invert toggle | Send same tone to both ins, sum at one out | 180° polarity check via cancellation |
| HPF freq sweep, fixed slope | Logarithmic chirp 20 Hz–20 kHz | Crossover frequency calibration |
| HPF slope 6/12/18/24/30/36/42/48 dB/oct | Sweep | Slope encoding (firmware claims up to 48) |
| PEQ band freq + gain + Q | Sweep, fit parametric filter | **Band count, addressing, encoding** |
| Compressor threshold / attack / release | Above-threshold tone, then off | Dynamic-range envelope |
| Spk_type (speaker role) cycle through 0..24 | Sweep | What each role actually does to the channel |
| **Probe unknown register writes** | Try, measure | **Discover undocumented features** |

## Use

```python
from tests.loopback.audio_io import (
    sine, log_sweep, mono_to_stereo,
    play_and_record, rms_dbfs, tone_level_at,
)
from dsp408 import Device

with Device.open() as dsp:
    dsp.connect()
    dsp.set_channel_volume(0, db=-12)   # ← set a DSP control
    cap = play_and_record(mono_to_stereo(sine(1000, 0.5, amp_dbfs=-20)))
    print(f"DSP OUT 1 level: {rms_dbfs(cap.in1):+.1f} dBFS")
```

## Caveats

- **Scarlett firmware "too old" warning** (kernel: `version 2100, need 2115`)
  — does NOT block basic audio I/O on Linux 6.8. Verified by selfloop test
  passing. May limit advanced device-side mixer features we don't use.
- Scarlett wants **S32_LE only** for both playback and capture. `audio_io.py`
  handles the float ↔ int32 conversion bit-perfectly.
- Default samplerate is 48 kHz. Device supports 44.1k–192k.
- Requires the user to be in the `audio` group (or run via `sg audio -c`).
