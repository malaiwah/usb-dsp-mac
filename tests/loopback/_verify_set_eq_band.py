"""End-to-end check of dsp408.Device.set_eq_band().

Drives the new high-level API (q-parameter, not raw bandwidth byte) and
verifies the device produces the expected peak using a single pink-noise
shot per setting.
"""
import sys, time
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
from audio_io import DEFAULT_SR, mono_to_stereo, play_and_record
from dsp408 import Device, enumerate_devices

SR = DEFAULT_SR
DSP_OUT_INDEX = 1


def pink_noise(n, amp_dbfs=-18.0, seed=0):
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    F = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    shape = np.ones_like(f); shape[1:] = 1.0 / np.sqrt(f[1:])
    F *= shape
    pink = np.fft.irfft(F, n=n)
    pink /= np.max(np.abs(pink))
    return (pink * 10 ** (amp_dbfs / 20.0)).astype(np.float32)


def welch(x, sr, nperseg=8192):
    win = np.hanning(nperseg); wn = (win * win).sum(); step = nperseg // 2
    nseg = 1 + (len(x) - nperseg) // step
    psd = np.zeros(nperseg // 2 + 1)
    for i in range(nseg):
        seg = x[i * step:i * step + nperseg] * win
        S = np.fft.rfft(seg); psd += (S.conj() * S).real
    psd /= (nseg * wn * sr)
    return np.fft.rfftfreq(nperseg, 1.0 / sr), psd


def measure():
    src = pink_noise(int(1.5 * SR), seed=42)
    cap = play_and_record(mono_to_stereo(src, left=True, right=False))
    body = slice(int(0.10 * SR), len(cap.in1) - int(0.05 * SR))
    f, P_in = welch(cap.in1[body], cap.sr)
    _, P_ref = welch(cap.lp1[body], cap.sr)
    return f, 10 * np.log10((P_in + 1e-20) / (P_ref + 1e-20))


def peak(f, H, fc, oct=2.0):
    m = (f >= fc / 2 ** oct) & (f <= fc * 2 ** oct)
    Hsm = np.convolve(H[m], np.ones(3) / 3, mode="same")
    i = int(np.argmax(Hsm))
    return float(Hsm[i]), float(f[m][i])


info = enumerate_devices()[0]
with Device.open(path=info["path"]) as dsp:
    dsp.connect()
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
    # Flatten all bands first
    for b in range(dsp.EQ_BAND_COUNT):
        dsp.set_eq_band(DSP_OUT_INDEX, b, dsp.EQ_DEFAULT_FREQS_HZ[b], 0.0)
        time.sleep(0.02)
    time.sleep(0.4)
    f0, H0 = measure()

    print(f"{'API call':<40} {'pk dB':>7} {'fc Hz':>7}")
    print("-" * 60)

    cases = [
        # (label, args)
        ("set_eq_band(1, 5, 1000, +12, q=5)",   dict(channel=1, band=5, freq_hz=1000, gain_db=+12, q=5)),
        ("set_eq_band(1, 5, 1000, +12, q=2)",   dict(channel=1, band=5, freq_hz=1000, gain_db=+12, q=2)),
        ("set_eq_band(1, 5, 1000, -6, q=10)",   dict(channel=1, band=5, freq_hz=1000, gain_db=-6, q=10)),
        ("set_eq_band(1, 3, 250,  +9, q=3)",    dict(channel=1, band=3, freq_hz=250,  gain_db=+9, q=3)),
    ]
    for label, kwargs in cases:
        # reset previous band's gain to flat first
        for b in range(dsp.EQ_BAND_COUNT):
            dsp.set_eq_band(DSP_OUT_INDEX, b, dsp.EQ_DEFAULT_FREQS_HZ[b], 0.0)
            time.sleep(0.02)
        time.sleep(0.3)
        dsp.set_eq_band(**kwargs); time.sleep(0.4)
        f, H = measure()
        Hd = H - H0
        pk, fc_meas = peak(f, Hd, kwargs["freq_hz"])
        print(f"{label:<40} {pk:+7.2f} {fc_meas:>7.0f}")

    # Check raw bandwidth_byte path matches q path: q=5 ↔ b4≈51 (256/5)
    for b in range(dsp.EQ_BAND_COUNT):
        dsp.set_eq_band(DSP_OUT_INDEX, b, dsp.EQ_DEFAULT_FREQS_HZ[b], 0.0)
        time.sleep(0.02)
    time.sleep(0.3)
    dsp.set_eq_band(channel=1, band=5, freq_hz=1000, gain_db=+12,
                    bandwidth_byte=51); time.sleep(0.4)
    f, H = measure(); pk, fc_meas = peak(f, H - H0, 1000)
    print(f"{'set_eq_band(.., bandwidth_byte=51)':<40} {pk:+7.2f} {fc_meas:>7.0f}")

    # Cleanup
    for b in range(dsp.EQ_BAND_COUNT):
        dsp.set_eq_band(DSP_OUT_INDEX, b, dsp.EQ_DEFAULT_FREQS_HZ[b], 0.0)
        time.sleep(0.02)
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)

# Verify Q ↔ b4 conversion math
print("\nQ ↔ b4 round-trip:")
for q in (0.5, 1, 2, 5, 10, 20, 50, 100):
    b4 = Device.q_to_bandwidth_byte(q)
    q_back = Device.bandwidth_byte_to_q(b4)
    print(f"  q={q:>5}  → b4={b4:>3d}  → q≈{q_back:>6.2f}")
