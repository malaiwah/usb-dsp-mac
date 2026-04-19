"""Pink-noise EQ characterization — one capture per setting.

Why pink noise (vs the discrete-tone sweep):
  * Single ~1 s capture replaces 19 sequential 200 ms tones (~10× faster)
  * Continuous magnitude response — finer resolution of the bell shape
  * Pink noise = equal energy per octave → uniform SNR on the log freq axis
    where EQ peaks live.
  * Dividing in1 PSD by lp1 PSD cancels the source spectrum exactly, so we
    don't even need pink to be flat — only stable.

Measurement chain:
  laptop-side audio_io plays pink noise into Scarlett OUT 1
    → DSP IN 1 → routed to DSP OUT 2 (with the EQ band under test)
    → Scarlett IN 1 (cap.in1)
  reference: Scarlett's loopback monitor (cap.lp1) sees the unprocessed source
  H(f) = PSD(in1) / PSD(lp1)  →  10·log10(H) = EQ magnitude response in dB

This script does two things:
  1.  b4 sweep (Q calibration): hold band5 at fc=1000 Hz / +12 dB; vary b4
      across {8, 16, 26, 52, 78, 104, 156, 208}; measure each peak's
      -3 dB bandwidth and check the Q × b4 ≈ 260 law from the discrete-tone run.
  2.  Multiband sanity: enable bands 2/5/8 simultaneously (250 / 1000 / 4000 Hz)
      at +6 dB each, b4=52, and confirm we see three distinct peaks.

Output: prints per-config peak gain, -3, -6, -12 dB BW, and Q estimate.
"""
import sys, time
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
from audio_io import DEFAULT_SR, mono_to_stereo, play_and_record
from dsp408 import Device, enumerate_devices
from dsp408.protocol import CAT_PARAM, CMD_WRITE_EQ_BAND_BASE

SR = DEFAULT_SR
DSP_OUT_INDEX = 1     # OUT 2
NOISE_DUR_S = 1.5
NOISE_AMP_DBFS = -18.0


def pink_noise(n, amp_dbfs=-18.0, seed=0):
    """Voss-McCartney-ish pink noise via 1/sqrt(f) shaping in the freq domain."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    F = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    # 1/sqrt(f) shaping (avoid f=0)
    shape = np.ones_like(f)
    shape[1:] = 1.0 / np.sqrt(f[1:])
    F *= shape
    pink = np.fft.irfft(F, n=n)
    pink /= np.max(np.abs(pink))
    pink *= 10.0 ** (amp_dbfs / 20.0)
    return pink.astype(np.float32)


def write_eq_band(d, ch, band, freq, gain_db, b4):
    raw = max(0, min(1200, round(gain_db * 10 + 600)))
    payload = bytes([
        freq & 0xFF, (freq >> 8) & 0xFF,
        raw & 0xFF, (raw >> 8) & 0xFF,
        b4, 0, 0, 0,
    ])
    cmd = CMD_WRITE_EQ_BAND_BASE + (band << 8) + ch
    d.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)


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
    dsp.set_crossover(DSP_OUT_INDEX, 10, 0, 0, 22000, 0, 0)
    # Flatten all 10 EQ bands on the channel under test
    for b in range(10):
        # Use the firmware default centers so we don't perturb anything else
        default_f = (31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)[b]
        write_eq_band(dsp, DSP_OUT_INDEX, b, default_f, 0.0, 0x34)
        time.sleep(0.02)
    time.sleep(0.3)


def welch_psd(x, sr, nperseg=8192):
    """Hand-rolled Welch (50% overlap, Hann) — avoids scipy dependency."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < nperseg:
        nperseg = 1 << (len(x) - 1).bit_length() >> 1
    win = np.hanning(nperseg)
    win_norm = (win * win).sum()
    step = nperseg // 2
    nseg = 1 + (len(x) - nperseg) // step
    psd = np.zeros(nperseg // 2 + 1)
    for i in range(nseg):
        seg = x[i * step : i * step + nperseg] * win
        S = np.fft.rfft(seg)
        psd += (S.conj() * S).real
    psd /= (nseg * win_norm * sr)
    f = np.fft.rfftfreq(nperseg, 1.0 / sr)
    return f, psd


def measure_response():
    """Play one pink burst, return (freq Hz, dB) ratio in1/lp1."""
    src = pink_noise(int(NOISE_DUR_S * SR), amp_dbfs=NOISE_AMP_DBFS, seed=42)
    cap = play_and_record(mono_to_stereo(src, left=True, right=False))
    n_lead = int(0.10 * SR); n_tail = int(0.05 * SR)
    body = slice(n_lead, len(cap.in1) - n_tail)
    f, P_in = welch_psd(cap.in1[body], cap.sr, nperseg=8192)
    _, P_ref = welch_psd(cap.lp1[body], cap.sr, nperseg=8192)
    eps = 1e-20
    H_db = 10.0 * np.log10((P_in + eps) / (P_ref + eps))
    return f, H_db


def measure_peak(f, H_db, fc, search_octaves=2.0):
    """Around fc, find peak gain and -3/-6/-12 dB bandwidths and estimate Q."""
    lo = fc / (2 ** search_octaves); hi = fc * (2 ** search_octaves)
    m = (f >= lo) & (f <= hi)
    fseg, Hseg = f[m], H_db[m]
    if len(fseg) < 3:
        return {"peak_db": float("nan"), "fc_meas": fc, "bw3": float("nan"),
                "bw6": float("nan"), "bw12": float("nan"), "Q3": float("nan")}
    # Smooth lightly to defeat noise wiggles (3-bin running mean)
    Hsm = np.convolve(Hseg, np.ones(3) / 3, mode="same")
    i_pk = int(np.argmax(Hsm))
    peak_db = float(Hsm[i_pk])
    fc_meas = float(fseg[i_pk])

    def _bw(drop):
        target = peak_db - drop
        # walk left until below target
        i = i_pk
        while i > 0 and Hsm[i] >= target: i -= 1
        f_lo = fseg[i] if i > 0 else float("nan")
        i = i_pk
        while i < len(Hsm) - 1 and Hsm[i] >= target: i += 1
        f_hi = fseg[i] if i < len(Hsm) - 1 else float("nan")
        return f_hi - f_lo
    bw3 = _bw(3.0); bw6 = _bw(6.0); bw12 = _bw(12.0)
    Q3 = fc_meas / bw3 if bw3 > 0 else float("nan")
    return dict(peak_db=peak_db, fc_meas=fc_meas, bw3=bw3, bw6=bw6, bw12=bw12, Q3=Q3)


info = enumerate_devices()[0]
with Device.open(path=info["path"]) as dsp:
    dsp.connect()
    setup(dsp)

    # Baseline: all flat
    f, H0 = measure_response()
    peak_b = measure_peak(f, H0, 1000)
    print(f"\nBaseline (all bands flat):")
    print(f"  pk={peak_b['peak_db']:+5.2f} dB at {peak_b['fc_meas']:.0f} Hz  "
          f"(should be ~0 dB, no real peak)")

    # b4 sweep at band5 fc=1000 Hz / +12 dB
    print(f"\n=== b4 sweep at band5 fc=1000 Hz / +12 dB ===")
    print(f"{'b4 dec':>6} {'b4 hex':>7} {'pk dB':>7} {'fc Hz':>7} "
          f"{'BW-3':>6} {'BW-6':>7} {'BW-12':>8} {'Q(BW3)':>8} {'b4·Q':>8}")
    print("-" * 78)
    rows = []
    for b4 in [8, 16, 26, 39, 52, 78, 104, 156, 208]:
        write_eq_band(dsp, DSP_OUT_INDEX, band=5, freq=1000, gain_db=+12, b4=b4)
        time.sleep(0.4)
        f, H = measure_response()
        # subtract baseline so we look at the relative EQ response only
        Hd = H - H0
        r = measure_peak(f, Hd, 1000)
        rows.append((b4, r))
        print(f"{b4:>6d} {b4:>#7x} {r['peak_db']:+7.2f} {r['fc_meas']:>7.0f} "
              f"{r['bw3']:>6.0f} {r['bw6']:>7.0f} {r['bw12']:>8.0f} "
              f"{r['Q3']:>8.2f} {b4 * r['Q3']:>8.1f}")

    # Reset band5
    write_eq_band(dsp, DSP_OUT_INDEX, band=5, freq=1000, gain_db=0, b4=0x34)
    time.sleep(0.3)

    # Multiband sanity: bands 2/5/8 each +6 dB at default fcs (250/1000/4000)
    print(f"\n=== Multiband: bands 2/5/8 each +6 dB, b4=0x34 ===")
    write_eq_band(dsp, DSP_OUT_INDEX, band=2, freq=250,  gain_db=+6, b4=0x34); time.sleep(0.05)
    write_eq_band(dsp, DSP_OUT_INDEX, band=5, freq=1000, gain_db=+6, b4=0x34); time.sleep(0.05)
    write_eq_band(dsp, DSP_OUT_INDEX, band=8, freq=4000, gain_db=+6, b4=0x34); time.sleep(0.4)
    f, Hm = measure_response()
    Hd = Hm - H0
    for fc in (250, 1000, 4000):
        r = measure_peak(f, Hd, fc, search_octaves=0.7)
        print(f"  fc={fc:>5} Hz → pk={r['peak_db']:+5.2f} dB at {r['fc_meas']:>5.0f} Hz "
              f"(Q={r['Q3']:.2f})")

    # Restore
    for b in (2, 5, 8):
        default_f = (31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)[b]
        write_eq_band(dsp, DSP_OUT_INDEX, b, default_f, 0.0, 0x34)
        time.sleep(0.02)
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)

print("\n=== Summary ===")
print("If b4 · Q ≈ 260 holds across the whole sweep, the firmware encodes Q via")
print("a 1/x bandwidth byte (so the GUI's Q slider is roughly 260/b4).")
