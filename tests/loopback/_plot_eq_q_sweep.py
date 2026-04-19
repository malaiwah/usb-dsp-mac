"""Generate the EQ Q-sweep response plot for docs/measurements/.

Runs the same pink-noise probe as _probe_eq_pink.py but saves the
per-b4 magnitude curves to a PNG instead of just printing peak/BW
numbers. Output: /tmp/eq_band_q_sweep.png (then scp'd back).
"""
import sys, time
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from audio_io import DEFAULT_SR, mono_to_stereo, play_and_record
from dsp408 import Device, enumerate_devices

SR = DEFAULT_SR
DSP_OUT_INDEX = 1


def pink(n, dbfs=-18.0, seed=0):
    rng = np.random.default_rng(seed)
    F = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1.0 / SR)
    sh = np.ones_like(f); sh[1:] = 1.0 / np.sqrt(f[1:])
    F *= sh
    x = np.fft.irfft(F, n=n); x /= np.max(np.abs(x))
    return (x * 10 ** (dbfs / 20)).astype(np.float32)


def welch(x, sr, nperseg=8192):
    win = np.hanning(nperseg); wn = (win * win).sum(); step = nperseg // 2
    nseg = 1 + (len(x) - nperseg) // step
    psd = np.zeros(nperseg // 2 + 1)
    for i in range(nseg):
        seg = x[i * step:i * step + nperseg] * win
        S = np.fft.rfft(seg); psd += (S.conj() * S).real
    psd /= (nseg * wn * sr); return np.fft.rfftfreq(nperseg, 1.0 / sr), psd


def measure(seed=42):
    cap = play_and_record(mono_to_stereo(pink(int(1.5 * SR), seed=seed),
                                          left=True, right=False))
    body = slice(int(0.10 * SR), len(cap.in1) - int(0.05 * SR))
    f, P = welch(cap.in1[body], cap.sr); _, R = welch(cap.lp1[body], cap.sr)
    return f, 10 * np.log10((P + 1e-20) / (R + 1e-20))


info = enumerate_devices()[0]
B4_VALUES = [8, 16, 26, 52, 78, 104, 156, 208]

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
    for b in range(dsp.EQ_BAND_COUNT):
        dsp.set_eq_band(DSP_OUT_INDEX, b, dsp.EQ_DEFAULT_FREQS_HZ[b], 0.0)
        time.sleep(0.02)
    time.sleep(0.4)
    f0, H0 = measure()

    curves = []
    for b4 in B4_VALUES:
        dsp.set_eq_band(channel=DSP_OUT_INDEX, band=5,
                        freq_hz=1000, gain_db=+12, bandwidth_byte=b4)
        time.sleep(0.4)
        f, H = measure()
        curves.append((b4, f, H - H0))

    # Cleanup
    for b in range(dsp.EQ_BAND_COUNT):
        dsp.set_eq_band(DSP_OUT_INDEX, b, dsp.EQ_DEFAULT_FREQS_HZ[b], 0.0)
        time.sleep(0.02)
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)

# Plot
fig, ax = plt.subplots(figsize=(11, 6))
cmap = plt.cm.viridis
for i, (b4, f, H) in enumerate(curves):
    Q_est = 256.0 / b4
    color = cmap(i / max(len(curves) - 1, 1))
    ax.semilogx(f, H, color=color, lw=1.5,
                label=f"b4={b4:3d} (0x{b4:02x})  Q≈{Q_est:.1f}")

ax.axhline(0, color="gray", lw=0.5, ls=":")
ax.axhline(12, color="gray", lw=0.5, ls=":")
ax.axhline(9,  color="gray", lw=0.5, ls=":")  # -3 dB from peak
ax.axvline(1000, color="red", lw=0.5, ls=":", alpha=0.4)
ax.set_xlim(50, 20000)
ax.set_ylim(-2, 15)
ax.set_xlabel("Frequency (Hz)")
ax.set_ylabel("Gain relative to baseline (dB)")
ax.set_title("DSP-408 parametric EQ — band 5 fc=1000 Hz / +12 dB, "
             "Q controlled by bandwidth byte (b4)\n"
             "Q × b4 ≈ 256  →  fixed-point reciprocal encoding")
ax.legend(loc="upper right", fontsize=9, framealpha=0.85)
ax.grid(True, which="both", alpha=0.3)
plt.tight_layout()
out = "/tmp/eq_band_q_sweep.png"
plt.savefig(out, dpi=110)
print(f"Saved {out}")
