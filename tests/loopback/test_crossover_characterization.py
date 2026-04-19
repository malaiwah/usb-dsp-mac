"""DSP-408 crossover characterization via Scarlett loopback (discrete tones).

Two goals:

1. **Verify** that the wire-validated ``set_crossover()`` encoding produces
   the intended *acoustic* effect — that the cutoff frequencies, slopes,
   and filter-type bytes we write actually steer the DSP's IIR filters
   the way the Windows GUI does.

2. **Fingerprint filter type 3 ("Defeat" in the Windows UI)** — the
   captures + Android-app decompile only name three filter types
   (0=Butterworth, 1=Bessel, 2=Linkwitz-Riley); the Windows GUI also
   exposes "Defeat" (byte value 3). In pro-audio DSPs, "Defeat" usually
   means *bypass* (flat magnitude), but it can also mean *all-pass* (flat
   magnitude, phase shift only). Discrete-tone measurement tells us which:
   if the magnitude curve is flat across the whole band with type=3, it's
   a bypass; same shape as types 0..2 = something else.

Method (discrete-tone sweep — bulletproof vs noise):
  - For each filter type, walk a list of log-spaced test frequencies.
  - At each frequency: play a 200 ms sine on Scarlett OUT 1 → DSP IN 1
    → routed IN1→OUT2 → Scarlett IN 2; measure the level at the test
    freq via Hann-windowed FFT (``tone_level_at``).
  - First pass: WIDE-OPEN filters (HPF=10 Hz, LPF=22000 Hz, 6 dB/oct).
    All test frequencies fall inside the passband, so this captures the
    analog gain of the whole rig — which is what we subtract off.
    (Note: slope=8 also bypasses the filter cleanly — both a wide-open
    real filter and slope=8 give identical flat passbands. Either works
    as the baseline; we use the wide-open form here so the analysis is
    unaffected if the slope=8 semantic ever changes.)
  - Subsequent passes: subtract baseline → pure DSP filter response in dB
    relative to passband, independent of analog calibration.

Setup (mono): Scarlett OUT 1 → DSP IN 1; DSP OUT 2 → Scarlett IN 1.
(DSP OUT 2 is the only DSP output wired back to the rig.)

Run on the rig:
    sg audio -c '.venv/bin/python tests/loopback/test_crossover_characterization.py'

Output:
  - Console: per-type knee freq, passband ripple, asymptotic slopes.
  - PNG: /tmp/dsp408_crossover_types_<timestamp>.png — all 4 filter
    types overlaid (relative to baseline).
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/mbelleau/dsp408")

import numpy as np

from audio_io import (
    DEFAULT_SR,
    mono_to_stereo,
    play_and_record,
    sine,
    tone_level_at,
)

from dsp408 import Device, enumerate_devices

# ── Rig wiring ────────────────────────────────────────────────────────────
SCARLETT_OUT_IDX = 0      # Scarlett OUT 1
DSP_OUT_INDEX = 1         # DSP OUT 2 (the channel wired back to Scarlett IN 2)
DSP_IN_FOR_ROUTE = 1      # DSP IN 1 feeds OUT 2

# ── Tone parameters ───────────────────────────────────────────────────────
SR = DEFAULT_SR
TONE_DUR_S = 0.20         # 200 ms — long enough for 5 Hz FFT bin width
TONE_AMP_DBFS = -20.0     # well below clipping with headroom for filter Q

# 30 log-spaced test frequencies, 30 Hz to 18 kHz (~9 octaves).
# Tighter spacing near the knees (200 Hz, 5 kHz) to resolve the slope.
TEST_FREQS_HZ = sorted(set(int(f) for f in np.concatenate([
    np.logspace(np.log10(30), np.log10(18000), 24),
    [80, 120, 150, 175, 200, 225, 250, 300, 350, 400, 500, 700,
     2500, 3500, 4000, 4500, 5000, 5500, 6000, 7000, 9000, 12000],
])))

# ── Test points ───────────────────────────────────────────────────────────
HPF_FREQ = 200
LPF_FREQ = 5000
SLOPE = 3                  # slope code: 0=6, 1=12, 2=18, 3=24 dB/oct

FILTER_NAMES = {
    0: "Butterworth",
    1: "Bessel",
    2: "Linkwitz-Riley",
    3: "Defeat",   # ← from Windows UI dropdown; identity TBD by this test
}
SLOPE_DB = {0: 6, 1: 12, 2: 18, 3: 24, 4: 30, 5: 36, 6: 42, 7: 48}


# ── Setup helpers ─────────────────────────────────────────────────────────
def setup_dsp(dsp: Device) -> None:
    """Master to 0 dB, route IN1 → OUT2, mute everything else."""
    dsp.set_master(db=0.0, muted=False)

    # Warmup: avoid the firmware startup write-drop quirk by interleaving
    # set+read on the channel under test (per device.set_channel docstring).
    for _ in range(8):
        dsp.set_channel(DSP_OUT_INDEX, db=0.0, muted=False)
        time.sleep(0.05)
        dsp.read_channel_state(DSP_OUT_INDEX)

    # Mute every channel we're not measuring.
    for ch in range(8):
        if ch == DSP_OUT_INDEX:
            continue
        dsp.set_channel(ch, db=0.0, muted=True)

    # Clear all routing, then connect IN1 → OUT2 only.
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
    dsp.set_routing(DSP_OUT_INDEX,
                    in1=(DSP_IN_FOR_ROUTE == 1),
                    in2=(DSP_IN_FOR_ROUTE == 2),
                    in3=(DSP_IN_FOR_ROUTE == 3),
                    in4=(DSP_IN_FOR_ROUTE == 4))
    time.sleep(0.2)


def measure_tone(freq_hz: float) -> tuple[float, float]:
    """Play one sine tone, return (ref_dbfs, sig_dbfs).

    ref = Scarlett's internal loopback of OUT 1 (= the tone we played).
    sig = analog IN 2 (= post-DSP-filter signal).
    """
    tone = sine(freq_hz, TONE_DUR_S, amp_dbfs=TONE_AMP_DBFS)
    play = mono_to_stereo(tone, left=True, right=False)
    cap = play_and_record(play)
    # Trim leading/trailing 50 ms to avoid the play_and_record padding +
    # any analog/IIR transient.
    n_lead = int(0.06 * SR)
    n_tail = int(0.04 * SR)
    body = slice(n_lead, len(cap) - n_tail)
    bw = max(8.0, freq_hz * 0.02)  # ±2 % bandwidth, min 8 Hz
    ref_db = tone_level_at(cap.lp1[body], cap.sr, freq_hz, bw_hz=bw)
    # DSP OUT 2 → Scarlett IN 1 (the only DSP output wired back; per
    # test_gain_calibration's "Sink: Scarlett IN 1 (= DSP OUT 2)" note).
    sig_db = tone_level_at(cap.in1[body], cap.sr, freq_hz, bw_hz=bw)
    return ref_db, sig_db


def measure_filter_curve(
    dsp: Device,
    hpf_freq: int, hpf_filter: int, hpf_slope: int,
    lpf_freq: int, lpf_filter: int, lpf_slope: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Set crossover, sweep tones, return (freqs_hz, gain_db_relative).

    Gain is reported as ``sig_dbfs - ref_dbfs`` so it represents the
    transfer function through the rig (analog + DSP). Subtract the
    baseline (filters off) from each result to isolate the DSP filter.
    """
    dsp.set_crossover(DSP_OUT_INDEX,
                      hpf_freq, hpf_filter, hpf_slope,
                      lpf_freq, lpf_filter, lpf_slope)
    time.sleep(0.4)  # IIR coefficient swap settling

    freqs = []
    gains = []
    for f in TEST_FREQS_HZ:
        ref_db, sig_db = measure_tone(float(f))
        freqs.append(float(f))
        gains.append(sig_db - ref_db)
    return np.array(freqs), np.array(gains)


# ── Curve analysis ────────────────────────────────────────────────────────
def find_knee(freqs: np.ndarray, gain_db: np.ndarray,
              passband_lo: float, passband_hi: float,
              drop_db: float) -> dict:
    """Locate the HPF + LPF knees at -drop_db relative to passband level.

    Passband level = median gain in the [passband_lo, passband_hi] window.
    """
    pb_mask = (freqs >= passband_lo) & (freqs <= passband_hi)
    if not pb_mask.any():
        return {}
    pb_level = float(np.median(gain_db[pb_mask]))
    target = pb_level - drop_db
    knees: dict = {"passband_db": pb_level}

    # HPF knee: walk down from passband_lo, find first crossing of `target`.
    below_idx = np.where(freqs <= passband_lo)[0]
    for i in reversed(below_idx):
        if gain_db[i] <= target and i + 1 < len(freqs):
            f0, f1 = freqs[i], freqs[i + 1]
            g0, g1 = gain_db[i], gain_db[i + 1]
            if g1 != g0:
                # log-frequency interpolation (filters are linear in log f)
                lf = np.log(f0) + (target - g0) / (g1 - g0) * (np.log(f1) - np.log(f0))
                knees["hpf_hz"] = float(np.exp(lf))
            else:
                knees["hpf_hz"] = float(f0)
            break

    # LPF knee
    above_idx = np.where(freqs >= passband_hi)[0]
    for i in above_idx:
        if gain_db[i] <= target and i > 0:
            f0, f1 = freqs[i - 1], freqs[i]
            g0, g1 = gain_db[i - 1], gain_db[i]
            if g1 != g0:
                lf = np.log(f0) + (target - g0) / (g1 - g0) * (np.log(f1) - np.log(f0))
                knees["lpf_hz"] = float(np.exp(lf))
            else:
                knees["lpf_hz"] = float(f1)
            break

    return knees


def fit_slope_db_per_oct(freqs: np.ndarray, gain_db: np.ndarray,
                         f_lo: float, f_hi: float) -> float:
    """Linear fit of gain_db vs log2(f) over [f_lo, f_hi]; return slope dB/oct."""
    mask = (freqs >= f_lo) & (freqs <= f_hi) & (gain_db > -50) & np.isfinite(gain_db)
    if mask.sum() < 3:
        return float("nan")
    x = np.log2(freqs[mask])
    y = gain_db[mask]
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def passband_ripple_db(freqs: np.ndarray, gain_db: np.ndarray,
                       passband_lo: float, passband_hi: float) -> float:
    mask = (freqs >= passband_lo) & (freqs <= passband_hi)
    if not mask.any():
        return float("nan")
    return float(np.max(gain_db[mask]) - np.min(gain_db[mask]))


# ── Main ──────────────────────────────────────────────────────────────────
def main() -> None:
    devs = enumerate_devices()
    if not devs:
        sys.exit("no DSP-408 found")
    info = devs[0]
    print(f"DSP: {info['display_id']}  path={info['path']!r}")
    print(f"Rig: Scarlett OUT 1 → DSP IN 1 → OUT 2 → Scarlett IN 2")
    print(f"Tones: {len(TEST_FREQS_HZ)} freqs, {TONE_DUR_S * 1000:.0f} ms each, "
          f"{TONE_AMP_DBFS:+.0f} dBFS")
    print(f"Test:  HPF {HPF_FREQ} Hz / LPF {LPF_FREQ} Hz / "
          f"slope {SLOPE_DB[SLOPE]} dB/oct\n")

    with Device.open(path=info["path"]) as dsp:
        dsp.connect()
        setup_dsp(dsp)

        # Baseline: wide-open Butterworth (HPF=10 Hz, LPF=22000 Hz, 6 dB/oct).
        # Test frequencies (30..18000 Hz) fall inside the passband, so this
        # captures the analog gain of the rig — subtract from each test for
        # the pure DSP filter response.  (slope=8 also works as a bypass
        # baseline and gives the same flat curve; this form is more
        # explicit about its intent.)
        print("=== Baseline: wide-open Butterworth (HPF 10 Hz / LPF 22000 Hz / 6 dB/oct) ===")
        baseline_f, baseline_g = measure_filter_curve(
            dsp, 10, 0, 0, 22000, 0, 0,
        )
        for f, g in zip(baseline_f, baseline_g):
            print(f"    {f:>6.0f} Hz  {g:+7.2f} dB")
        baseline_pb = float(np.median(
            baseline_g[(baseline_f >= 500) & (baseline_f <= 2000)]
        ))
        print(f"  baseline passband (500-2000 Hz median) = {baseline_pb:+.2f} dB")
        print(f"  baseline ripple over 30..18000 Hz = "
              f"{baseline_g.max() - baseline_g.min():.2f} dB\n")

        results: dict[int, tuple[np.ndarray, np.ndarray]] = {}

        for ftype in (0, 1, 2, 3):
            print(f"=== Filter type {ftype} ({FILTER_NAMES[ftype]}) ===")
            freqs, gain_db = measure_filter_curve(
                dsp,
                HPF_FREQ, ftype, SLOPE,
                LPF_FREQ, ftype, SLOPE,
            )
            # Subtract baseline → relative dB (DSP filter only)
            rel_db = gain_db - baseline_g
            results[ftype] = (freqs, rel_db)

            pb_lo, pb_hi = HPF_FREQ * 2.5, LPF_FREQ / 2.5
            knees3 = find_knee(freqs, rel_db, pb_lo, pb_hi, drop_db=3.0)
            knees6 = find_knee(freqs, rel_db, pb_lo, pb_hi, drop_db=6.0)
            ripple = passband_ripple_db(freqs, rel_db, pb_lo, pb_hi)
            hpf_slope = fit_slope_db_per_oct(freqs, rel_db,
                                             HPF_FREQ * 0.20, HPF_FREQ * 0.55)
            lpf_slope = fit_slope_db_per_oct(freqs, rel_db,
                                             LPF_FREQ * 1.8, LPF_FREQ * 3.0)

            print(f"  passband level   = {knees3.get('passband_db', float('nan')):+.2f} dB (rel baseline)")
            print(f"  passband ripple ({pb_lo:.0f}..{pb_hi:.0f} Hz) = {ripple:.2f} dB")
            hpf3 = knees3.get('hpf_hz', float('nan'))
            lpf3 = knees3.get('lpf_hz', float('nan'))
            hpf6 = knees6.get('hpf_hz', float('nan'))
            lpf6 = knees6.get('lpf_hz', float('nan'))
            print(f"  -3 dB knee:  HPF={hpf3:>7.1f} Hz  LPF={lpf3:>7.1f} Hz")
            print(f"  -6 dB knee:  HPF={hpf6:>7.1f} Hz  LPF={lpf6:>7.1f} Hz")
            print(f"  asymptote:   HPF={hpf_slope:+.1f} dB/oct  "
                  f"LPF={lpf_slope:+.1f} dB/oct  "
                  f"(requested ±{SLOPE_DB[SLOPE]} dB/oct)")
            print()

        # Plot all four overlaid
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(11, 6))
            for ftype, (f, g) in results.items():
                ax.semilogx(f, g,
                            label=f"type {ftype} — {FILTER_NAMES[ftype]}",
                            linewidth=1.6, marker="o", markersize=3)
            ax.axvline(HPF_FREQ, color="grey", linestyle="--", alpha=0.5,
                       label=f"HPF {HPF_FREQ} Hz")
            ax.axvline(LPF_FREQ, color="grey", linestyle=":", alpha=0.5,
                       label=f"LPF {LPF_FREQ} Hz")
            ax.axhline(-3, color="red", linestyle="--", alpha=0.3)
            ax.axhline(-6, color="red", linestyle=":", alpha=0.3)
            ax.set_xlabel("Frequency (Hz)")
            ax.set_ylabel("Gain (dB, relative to filters-off baseline)")
            ax.set_title(
                f"DSP-408 crossover filter types — "
                f"HPF {HPF_FREQ} Hz / LPF {LPF_FREQ} Hz / "
                f"{SLOPE_DB[SLOPE]} dB/oct"
            )
            ax.set_ylim(-50, 5)
            ax.set_xlim(20, 20000)
            ax.grid(True, which="both", alpha=0.3)
            ax.legend(loc="lower center", ncol=2)
            ts = int(time.time())
            png_path = f"/tmp/dsp408_crossover_types_{ts}.png"
            fig.savefig(png_path, dpi=120, bbox_inches="tight")
            print(f"  saved plot: {png_path}")
        except ImportError:
            print("  (matplotlib not available — skipped plot)")

        # Restore defaults
        print("\nrestoring defaults...")
        dsp.set_crossover(DSP_OUT_INDEX, 20, 0, 1, 20000, 0, 1)
        for ch in range(8):
            dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
            dsp.set_channel(ch, db=0.0, muted=False)


if __name__ == "__main__":
    main()
