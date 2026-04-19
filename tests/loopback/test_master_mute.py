"""DSP-408 master mute round-trip verification.

Master mute lives in byte[6] of the cmd=0x05 payload (cat=0x09):
  * audible bit: 1 = unmuted, 0 = muted (note inverted polarity vs
    per-channel byte[0])

This test:
  1. Plays a 1 kHz tone with master unmuted, measures level.
  2. Asserts get_master() returns muted=False.
  3. Mutes the master via set_master_mute(True).
  4. Plays the same tone — should drop to noise floor.
  5. Asserts get_master() returns muted=True (read-after-write).
  6. Unmutes, plays again — should return to ~original level.
  7. Asserts get_master() returns muted=False again.

Setup (mono): Scarlett OUT 1 → DSP IN → DSP routes IN1→OUT2 → Scarlett IN1.

Run:
    sg audio -c '.venv/bin/python tests/loopback/test_master_mute.py'
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
DSP_OUT_INDEX = 1
DSP_IN_FOR_ROUTE = 1


def play_one(out_idx: int):
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
    print(f"DSP: {info['display_id']} path={info['path']!r}\n")

    failures: list[str] = []

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        # Setup
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_routing(DSP_OUT_INDEX,
                        in1=(DSP_IN_FOR_ROUTE == 1),
                        in2=(DSP_IN_FOR_ROUTE == 2),
                        in3=(DSP_IN_FOR_ROUTE == 3),
                        in4=(DSP_IN_FOR_ROUTE == 4))

        # ── 1. Unmuted baseline ────────────────────────────────────────
        dsp.set_master(db=0.0, muted=False)
        time.sleep(0.1)
        db, muted = dsp.get_master()
        print(f"State after set_master(db=0, muted=False): "
              f"db={db:+.1f}, muted={muted}")
        if muted is not False:
            failures.append(f"get_master() returned muted={muted}, "
                            f"expected False")
        cap = play_one(SCARLETT_OUT_IDX)
        ref_dbfs = measure(cap)
        print(f"  IN1 tone level: {ref_dbfs:+6.1f} dBFS  (reference)\n")
        if ref_dbfs < -50:
            sys.exit(f"!! reference level {ref_dbfs:+.1f} dBFS too low — "
                     "check signal flow before testing mute")

        # ── 2. Mute and measure ────────────────────────────────────────
        dsp.set_master_mute(True)
        time.sleep(0.1)
        db, muted = dsp.get_master()
        print(f"State after set_master_mute(True): "
              f"db={db:+.1f}, muted={muted}")
        if muted is not True:
            failures.append(f"get_master() after mute=True returned "
                            f"muted={muted}, expected True")
        cap = play_one(SCARLETT_OUT_IDX)
        muted_dbfs = measure(cap)
        attenuation = ref_dbfs - muted_dbfs
        print(f"  IN1 tone level: {muted_dbfs:+6.1f} dBFS  "
              f"(attenuation: {attenuation:+.1f} dB)\n")
        # Expect tone to drop into noise floor; that's typically <-70 dBFS
        # vs -20 dBFS ref → attenuation > 40 dB. Use a permissive 30 dB
        # threshold to allow for noisy environments.
        if attenuation < 30:
            failures.append(f"mute attenuation only {attenuation:+.1f} dB; "
                            f"expected >30 dB drop")

        # ── 3. Unmute and verify recovery ──────────────────────────────
        dsp.set_master_mute(False)
        time.sleep(0.1)
        db, muted = dsp.get_master()
        print(f"State after set_master_mute(False): "
              f"db={db:+.1f}, muted={muted}")
        if muted is not False:
            failures.append(f"get_master() after mute=False returned "
                            f"muted={muted}, expected False")
        cap = play_one(SCARLETT_OUT_IDX)
        recovered_dbfs = measure(cap)
        delta_from_ref = recovered_dbfs - ref_dbfs
        print(f"  IN1 tone level: {recovered_dbfs:+6.1f} dBFS  "
              f"(delta vs ref: {delta_from_ref:+.2f} dB)\n")
        if abs(delta_from_ref) > 1.0:
            failures.append(f"recovered level off by {delta_from_ref:+.2f} dB; "
                            f"expected within ±1 dB of reference")

        # ── 4. Volume preservation across mute toggle ──────────────────
        # set_master_mute() reads-modify-writes; verify that toggling mute
        # at a non-zero volume preserves the volume.
        dsp.set_master(db=-12.0, muted=False)
        time.sleep(0.1)
        dsp.set_master_mute(True)
        time.sleep(0.05)
        dsp.set_master_mute(False)
        time.sleep(0.05)
        db, muted = dsp.get_master()
        print(f"After set_master(-12 dB), mute, unmute: "
              f"db={db:+.1f}, muted={muted}")
        if abs(db - (-12.0)) > 0.5:
            failures.append(f"volume drifted to {db:+.1f} dB after mute toggle "
                            f"at -12 dB; expected -12 dB preserved")
        if muted is not False:
            failures.append(f"end state muted={muted}; expected False")

        # Restore
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)

    # ── Verdict ──────────────────────────────────────────────────────
    print()
    print("=== Verdict ===")
    if not failures:
        print("  ✓ Master mute round-trip verified end-to-end:")
        print("    - set/get round-trip correct")
        print("    - mute silences audio (>30 dB attenuation)")
        print("    - unmute restores level (within ±1 dB)")
        print("    - volume preserved across mute toggle")
    else:
        print(f"  ✗ {len(failures)} failure(s):")
        for f in failures:
            print(f"    - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
