"""DSP-408 per-channel delay calibration via Scarlett loopback.

The 296-byte channel state blob exposes a u16 LE delay field at offset 250
(also written by the 8-byte ``cmd=0x1FNN`` basic-record at bytes [4..5]).
The published manual claims:

    0 to 277 cm  ⇔  0 to 8.1471 ms  ⇔  0 to ~391 samples @ 48 kHz

…but the firmware field is u16, so it physically can hold up to 65535. The
user noted the spec hints at "a bit beyond 8 ms" — this test maps both the
linear scale (samples vs. measured delay) AND the actual upper limit.

Method:
  - Play a short log-sweep chirp on Scarlett OUT 1 (great autocorrelation).
  - Capture LP1 (reference: what Scarlett actually played) and IN1
    (signal that came back through DSP IN → set_channel(delay) → DSP OUT).
  - Cross-correlate IN1 vs LP1 → find lag in samples.
  - Baseline lag at delay=0 = analog + USB + DSP fixed latency.
  - For each test value N, measured_extra = lag(N) − lag(0); should ≈ N.

Setup (mono): Scarlett OUT 1 → DSP IN → DSP routes IN1→OUT2 → Scarlett IN 1.

Run:
    sg audio -c '.venv/bin/python tests/loopback/test_delay_calibration.py'
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/mbelleau/dsp408")

import numpy as np
import sounddevice as sd

from dsp408 import Device, enumerate_devices

from audio_io import (
    CAPTURE_CHANNELS,
    DEFAULT_FORMAT,
    DEFAULT_SR,
    PLAYBACK_CHANNELS,
    find_scarlett,
    float_to_int32,
    int32_to_float,
    log_sweep,
    rms_dbfs,
)

SCARLETT_OUT_IDX = 0
DSP_OUT_INDEX = 1     # OUT 2 (the only one wired back)
DSP_IN_FOR_ROUTE = 1

# Short chirp = sharp autocorrelation peak. 50 ms × 200..8000 Hz.
CHIRP_DUR_S = 0.05
CHIRP_F0 = 200.0
CHIRP_F1 = 8000.0
CHIRP_AMP_DBFS = -12.0

# Manual upper bound = 391 samples (8.147 ms @ 48 kHz). Probe up to u16 max
# to find the actual firmware limit. Each value gets its own playback pass
# so the recording window can scale with it.
SWEEP_SAMPLES = [
    0,        # baseline
    24,       # 0.5 ms
    48,       # 1.0 ms
    96,       # 2.0 ms
    192,      # 4.0 ms
    288,      # 6.0 ms
    384,      # 8.0 ms  (manual max)
    480,      # 10 ms   (just past spec)
    960,      # 20 ms
    1920,     # 40 ms
    4800,     # 100 ms
    9600,     # 200 ms
    24000,    # 500 ms
    48000,    # 1.0 s
    65535,    # u16 max  (~1.366 s)
]


def play_chirp_with_delay_window(out_idx: int,
                                  expected_delay_samples: int,
                                  sr: int = DEFAULT_SR):
    """Play a short chirp and capture long enough to see a delayed echo.

    ``play_and_record`` uses fixed pads — we need the post-pad to scale
    with ``expected_delay_samples`` so the delayed copy actually lands
    inside the capture window. Implements the same pattern inline.
    """
    chirp = log_sweep(CHIRP_F0, CHIRP_F1, CHIRP_DUR_S,
                      amp_dbfs=CHIRP_AMP_DBFS, sr=sr)
    pre = np.zeros((int(0.05 * sr), PLAYBACK_CHANNELS), dtype=np.float64)
    body = np.zeros((len(chirp), PLAYBACK_CHANNELS), dtype=np.float64)
    body[:, out_idx] = chirp
    # Post pad: at least 250 ms baseline (analog + USB latency) + the
    # configured DSP delay + a comfortable trailer for the chirp tail.
    post_samples = int(0.30 * sr) + expected_delay_samples + len(chirp)
    post = np.zeros((post_samples, PLAYBACK_CHANNELS), dtype=np.float64)
    full_play = np.vstack([pre, body, post])
    playback_int32 = float_to_int32(full_play)
    captured = sd.playrec(
        playback_int32,
        samplerate=sr,
        channels=CAPTURE_CHANNELS,
        dtype=DEFAULT_FORMAT,
        device=find_scarlett(),
        blocking=True,
    )
    captured_f = int32_to_float(captured)
    return sr, captured_f[:, 0], captured_f[:, 2]   # in1, lp1


def cross_correlate_lag(reference: np.ndarray,
                         signal: np.ndarray) -> tuple[int, float]:
    """Find lag (samples) where ``signal`` best matches ``reference``.

    Positive lag means signal is delayed relative to reference. Uses an
    FFT-based correlation for speed on long buffers.

    Returns (lag_samples, normalised_peak_value).
    """
    n = len(reference) + len(signal)
    pad = 1 << (n - 1).bit_length()
    R = np.fft.rfft(reference, pad)
    S = np.fft.rfft(signal, pad)
    xcorr = np.fft.irfft(S * np.conj(R), pad)
    abs_xc = np.abs(xcorr)
    peak = int(np.argmax(abs_xc))
    # Wrap unsigned bin → signed lag in (-pad/2, +pad/2]
    if peak > pad // 2:
        peak -= pad
    # Normalise the peak by reference + signal energy (gives a 0..1 measure)
    norm = float(np.sqrt(np.sum(reference ** 2) * np.sum(signal ** 2)))
    return peak, (abs_xc.max() / norm) if norm > 0 else 0.0


def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found")
    info = devs[0]
    print(f"DSP: {info['display_id']} path={info['path']!r}")
    print(f"Sweeping delay_samples: {SWEEP_SAMPLES}\n")

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        dsp.set_master(db=0.0, muted=False)
        for ch in range(8):
            dsp.set_channel(ch, db=0.0, muted=False, delay_samples=0)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_routing(DSP_OUT_INDEX,
                        in1=(DSP_IN_FOR_ROUTE == 1),
                        in2=(DSP_IN_FOR_ROUTE == 2),
                        in3=(DSP_IN_FOR_ROUTE == 3),
                        in4=(DSP_IN_FOR_ROUTE == 4))
        time.sleep(0.1)

        # ── Baseline: delay=0 ────────────────────────────────────────
        dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False, delay_samples=0)
        time.sleep(0.1)
        sr, in1, lp1 = play_chirp_with_delay_window(SCARLETT_OUT_IDX, 0)
        in1_db = rms_dbfs(in1)
        if in1_db < -60:
            sys.exit(f"!! IN1 level {in1_db:+.1f} dBFS too low — check signal flow")
        baseline_lag, baseline_corr = cross_correlate_lag(lp1, in1)
        baseline_ms = baseline_lag * 1000.0 / sr
        print(f"Baseline (delay=0):  lag={baseline_lag:>5d} samples  "
              f"({baseline_ms:6.2f} ms)  corr={baseline_corr:.3f}  "
              f"IN1={in1_db:+.1f} dBFS\n")

        print(f"  {'requested':>10s} {'expected':>10s} {'measured':>10s} "
              f"{'extra':>10s} {'error':>9s} {'corr':>6s} {'verdict':>8s}")
        print(f"  {'(samples)':>10s} {'(ms)':>10s} {'(samples)':>10s} "
              f"{'(samples)':>10s} {'(samp)':>9s} {'':>6s} {'':>8s}")
        print(f"  {'─'*10} {'─'*10} {'─'*10} {'─'*10} {'─'*9} {'─'*6} {'─'*8}")

        results = []
        for req in SWEEP_SAMPLES:
            dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False, delay_samples=req)
            time.sleep(0.1)  # let device apply
            sr, in1, lp1 = play_chirp_with_delay_window(SCARLETT_OUT_IDX, req)
            lag, corr = cross_correlate_lag(lp1, in1)
            extra = lag - baseline_lag
            err = extra - req
            req_ms = req * 1000.0 / sr
            # Tolerate ±2 samples (~40 µs) for jitter. Empirically the
            # firmware caps at 359 taps; anything past that is "cap"
            # (expected behaviour, not a failure).
            HW_CEILING = 359
            if corr < 0.05:
                verdict = "?? noise"
            elif abs(err) <= 2:
                verdict = "✓"
            elif req > HW_CEILING and abs(extra - HW_CEILING) <= 1:
                verdict = "cap"
            elif abs(err) <= 20:
                verdict = "≈"
            else:
                verdict = "✗"
            results.append((req, req_ms, lag, extra, err, corr, verdict))
            print(f"  {req:>10d} {req_ms:>10.2f} {lag:>10d} {extra:>10d} "
                  f"{err:>+9d} {corr:>6.3f} {verdict:>8s}")

        # Restore: delay 0, routing off
        dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False, delay_samples=0)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)

    # ── Diagnose ─────────────────────────────────────────────────────
    print()
    # The known-good linear region (per bisection) is up to 358 samples,
    # ceiling at 359. Treat <=358 as "must be exact", >358 as "capped".
    LINEAR_MAX = 358
    HW_CEILING = 359

    linear = [r for r in results if 0 < r[0] <= LINEAR_MAX]
    capped = [r for r in results if r[0] > LINEAR_MAX]

    if linear:
        max_err = max(abs(r[4]) for r in linear)
        print(f"Linear region (1..{LINEAR_MAX} samples / 0..{LINEAR_MAX*1000.0/DEFAULT_SR:.2f} ms):  "
              f"max error = {max_err} samples  "
              f"({max_err * 1000.0 / DEFAULT_SR:.3f} ms)")
        if max_err <= 2:
            print("  ✓ delay_samples encoding is exact (1 sample = 1 sample).")
        elif max_err <= 20:
            print("  ≈ Mostly linear with measurable jitter.")
        else:
            print("  ✗ Delay scale doesn't match samples.")

    if capped:
        all_at_ceiling = all(abs(r[3] - HW_CEILING) <= 1 for r in capped)
        if all_at_ceiling:
            print(f"\nFirmware ceiling: {HW_CEILING} samples = "
                  f"{HW_CEILING*1000.0/DEFAULT_SR:.3f} ms @ 48 kHz "
                  f"({HW_CEILING*1000.0/44100:.3f} ms @ 44.1 kHz).")
            print("  ✓ Caps cleanly: any request > 358 silently saturates "
                  f"to {HW_CEILING} taps.")
            print("  ⚠ Manual quotes 8.1471 ms / 277 cm max — that figure "
                  "only holds at 44.1 kHz.")
        else:
            print(f"\n⚠ Out-of-spec probe didn't cap consistently at {HW_CEILING}.")
            for r in capped:
                print(f"  req={r[0]} measured_extra={r[3]}")


if __name__ == "__main__":
    main()
