"""Probe two open questions about the EQ:

  Q1. Are there hidden EQ slots beyond the 10 the Windows GUI exposes?
      Channel-state blob is 296 bytes; the basic record starts at byte 246
      → up to ~30 EQ bands of 8 bytes could fit at 0..245. Try writing
      band 10..30 at distinctive (freq, gain) and see if (a) the device
      ACKs, (b) the values land in the blob, and (c) we hear the peak.

  Q2. Do bands have to be in increasing-frequency order to work?
      I.e. is there an internal ordering constraint or are bands purely
      indexed slots? Write band 2 at 16000 Hz and band 7 at 100 Hz with
      both at +12 dB and check we still see both peaks at the right fcs.
"""
import sys, time
sys.path.insert(0, "/home/mbelleau/dsp408")
import numpy as np
from audio_io import DEFAULT_SR, mono_to_stereo, play_and_record
from dsp408 import Device, enumerate_devices
from dsp408.protocol import CAT_PARAM, CMD_WRITE_EQ_BAND_BASE

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
    psd /= (nseg * wn * sr)
    return np.fft.rfftfreq(nperseg, 1.0 / sr), psd


def measure(seed=42):
    cap = play_and_record(mono_to_stereo(pink(int(1.5 * SR), seed=seed),
                                          left=True, right=False))
    body = slice(int(0.10 * SR), len(cap.in1) - int(0.05 * SR))
    f, P = welch(cap.in1[body], cap.sr)
    _, R = welch(cap.lp1[body], cap.sr)
    return f, 10 * np.log10((P + 1e-20) / (R + 1e-20))


def write_band(d, ch, band, freq, gain_db, b4=0x34):
    raw = max(0, min(1200, round(gain_db * 10 + 600)))
    p = bytes([freq & 0xFF, (freq >> 8) & 0xFF,
               raw & 0xFF, (raw >> 8) & 0xFF, b4, 0, 0, 0])
    cmd = CMD_WRITE_EQ_BAND_BASE + (band << 8) + ch
    d.write_raw(cmd=cmd, data=p, category=CAT_PARAM)


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
    # Flatten visible 10 bands
    defs = (31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
    for b in range(10):
        write_band(dsp, DSP_OUT_INDEX, b, defs[b], 0.0)
        time.sleep(0.02)
    time.sleep(0.4)


def peak_near(f, H, fc, oct=1.0):
    m = (f >= fc / 2 ** oct) & (f <= fc * 2 ** oct)
    Hsm = np.convolve(H[m], np.ones(3) / 3, mode="same")
    i = int(np.argmax(np.abs(Hsm)))
    return float(Hsm[i]), float(f[m][i])


info = enumerate_devices()[0]
with Device.open(path=info["path"]) as dsp:
    dsp.connect()
    setup(dsp)

    f0, H0 = measure()

    # ── Q1: Hidden bands ────────────────────────────────────────────
    print("=== Q1: hidden bands beyond index 9? ===")
    print("Write each candidate band at +12 dB, distinctive freq, then read")
    print("blob[band*8 .. band*8+8] and probe acoustic response.\n")
    print(f"{'band':>4} {'freq':>5} {'ack?':>5} {'blob_match':>11} "
          f"{'pk dB @ probe':>15}")
    print("-" * 60)
    test_bands = [(10, 800), (11, 1500), (12, 600), (15, 350),
                  (20, 750), (25, 1100), (30, 900)]
    for b, freq in test_bands:
        try:
            write_band(dsp, DSP_OUT_INDEX, b, freq, +12.0)
            time.sleep(0.3)
            ack = "ok"
        except Exception as e:
            ack = f"err:{e!s:.10}"
            print(f"{b:>4} {freq:>5} {ack:>5} {'-':>11} {'-':>15}")
            continue
        # Read blob and check the candidate offset
        blob = bytes(dsp.read_channel_state(DSP_OUT_INDEX))
        chunk = blob[b * 8:(b + 1) * 8] if (b + 1) * 8 <= len(blob) else b""
        expected = bytes([freq & 0xFF, (freq >> 8) & 0xFF,
                          0xd0, 0x02, 0x34, 0, 0, 0])
        match = "yes" if chunk == expected else f"no({chunk.hex()[:12]})"
        # Acoustic check: is there a peak near `freq`?
        f, H = measure()
        Hd = H - H0
        pk, fc_meas = peak_near(f, Hd, freq, oct=0.6)
        print(f"{b:>4} {freq:>5} {ack:>5} {match:>11} "
              f"{pk:+6.2f}@{fc_meas:>4.0f}Hz  ".rjust(15))
        # Reset
        try:
            write_band(dsp, DSP_OUT_INDEX, b, freq, 0.0); time.sleep(0.1)
        except Exception:
            pass

    # ── Q2: Out-of-order frequency assignment ───────────────────────
    print("\n=== Q2: bands at non-monotonic frequencies ===")
    print("Within the safely-validated 10-band range, write bands at")
    print("frequencies in REVERSE order: band 2 = 16 kHz, band 7 = 100 Hz.\n")
    # Reset all to flat first
    defs = (31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
    for b in range(10):
        write_band(dsp, DSP_OUT_INDEX, b, defs[b], 0.0); time.sleep(0.02)
    time.sleep(0.3)

    # Now: band 2 at 16000 Hz, band 7 at 100 Hz, both +12 dB
    write_band(dsp, DSP_OUT_INDEX, 2, 16000, +12.0); time.sleep(0.05)
    write_band(dsp, DSP_OUT_INDEX, 7, 100,   +12.0); time.sleep(0.4)

    blob = bytes(dsp.read_channel_state(DSP_OUT_INDEX))
    print(f"  band 2 chunk (offset 16..24): {blob[16:24].hex()}")
    print(f"  band 7 chunk (offset 56..64): {blob[56:64].hex()}")
    # Expected: band2 = 80 3e d0 02 34 00 00 00 (16000 Hz, +12 dB)
    #           band7 = 64 00 d0 02 34 00 00 00 (100 Hz, +12 dB)
    f, H = measure(); Hd = H - H0
    for fc in (100, 16000):
        pk, fc_meas = peak_near(f, Hd, fc, oct=0.7)
        print(f"  expected peak @ {fc:>5} Hz: measured {pk:+6.2f} dB @ {fc_meas:.0f} Hz")

    # Sanity: no spurious peak at original default centers (250 Hz / 4000 Hz)
    for fc in (250, 4000):
        pk, fc_meas = peak_near(f, Hd, fc, oct=0.3)
        print(f"  (default fc {fc:>5} Hz: pk={pk:+5.2f} dB — should be ~0)")

    # Reset
    for b in range(10):
        write_band(dsp, DSP_OUT_INDEX, b, defs[b], 0.0); time.sleep(0.02)
    for ch in range(8):
        dsp.set_routing(ch, in1=False, in2=False, in3=False, in4=False)
        dsp.set_channel(ch, db=0.0, muted=False)
