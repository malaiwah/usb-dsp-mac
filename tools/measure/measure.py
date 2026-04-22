"""Mac/Linux room-acoustics measurement — REW-compatible output.

Plays a Farina log sweep, captures a USB measurement mic, deconvolves
to an impulse response (spectral division with Tikhonov regularization),
windows + FFT to a frequency response, applies the mic's per-frequency
calibration, writes a REW-compatible .txt the rest of the toolchain can
analyze.

Dependencies: sounddevice, numpy, scipy.

Usage:
    pip install sounddevice numpy scipy
    python measure.py --title "L test" --output L_test.txt \\
        --cal-file ~/Downloads/7080334_90deg.txt

Defaults assume Mac with "External Headphones" output and "Umik-1" input
device. Override with --output-device / --input-device.

The 1-5 kHz region is used as the SPL reference for downstream comparison
scripts; absolute SPL is approximate (±3 dB vs a calibrated REW pistonphone
sweep) but relative shape and per-channel comparisons are tight (verified
within 0.5 dB across consecutive runs).
"""
from __future__ import annotations

import argparse
import datetime as _dt

import numpy as np
import sounddevice as sd


DEFAULT_OUTPUT_DEVICE = "External Headphones"
DEFAULT_INPUT_DEVICE = "Umik-1"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_SWEEP_LENGTH = 262144  # 256K
SWEEP_LEVEL_DBFS = -12.0
FADE_MS = 10

# UMIK-1 reference constants (per-mic Sens Factor + AGain are read from
# the cal-file header; these defaults are used if no cal file is given)
DEFAULT_AGAIN_DB = 18.0
DEFAULT_SENS_FACTOR_DB = 4.7489


def log_sweep(n_samples: int, fs: int, f_lo: float, f_hi: float,
              level_dbfs: float = -12.0, fade_ms: int = 10) -> np.ndarray:
    """Farina log-swept sine. Returns float32 mono, level-normalized."""
    t_total = n_samples / fs
    t = np.arange(n_samples) / fs
    k = t_total / np.log(f_hi / f_lo)
    sweep = np.sin(2 * np.pi * f_lo * k * (np.exp(t / k) - 1.0))
    n_fade = int(fade_ms * 1e-3 * fs)
    fade = 0.5 * (1 - np.cos(np.linspace(0, np.pi, n_fade)))
    sweep[:n_fade] *= fade
    sweep[-n_fade:] *= fade[::-1]
    amp = 10 ** (level_dbfs / 20)
    sweep *= amp / np.max(np.abs(sweep))
    return sweep.astype(np.float32)


def deconvolve(capture: np.ndarray, sweep: np.ndarray,
               regularization: float = 1e-4) -> np.ndarray:
    """Spectral-division deconvolution with Tikhonov regularization.

    H(f) = Y(f) · conj(X(f)) / (|X(f)|² + ε·max|X|²)
    """
    n_fft = 1 << int(np.ceil(np.log2(max(len(capture), len(sweep)) * 2)))
    Y = np.fft.rfft(capture, n_fft)
    X = np.fft.rfft(sweep, n_fft)
    eps = regularization * np.max(np.abs(X)) ** 2
    H = Y * np.conj(X) / (np.abs(X) ** 2 + eps)
    return np.fft.irfft(H, n_fft)


def load_mic_cal(path: str) -> tuple[np.ndarray, np.ndarray, dict]:
    """Parse UMIK-1-style .txt cal: returns (freqs_hz, correction_db, meta)."""
    freqs: list[float] = []
    corrs: list[float] = []
    meta: dict = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('"'):
                if "Sens Factor" in line:
                    try:
                        meta["sens_factor_db"] = float(
                            line.split("Sens Factor")[1].split("=")[1].split("dB")[0])
                    except (IndexError, ValueError):
                        pass
                if "AGain" in line:
                    try:
                        meta["again_db"] = float(
                            line.split("AGain")[1].split("=")[1].split("dB")[0])
                    except (IndexError, ValueError):
                        pass
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    freqs.append(float(parts[0]))
                    corrs.append(float(parts[1]))
                except ValueError:
                    pass
    return np.array(freqs), np.array(corrs), meta


def apply_mic_cal(freqs: np.ndarray, spl_db: np.ndarray,
                   cal_f: np.ndarray, cal_db: np.ndarray) -> np.ndarray:
    """Subtract per-frequency cal correction (interpolated in log-freq)."""
    log_cal_f = np.log(cal_f)
    log_meas_f = np.log(np.maximum(freqs, 1e-9))
    interp = np.interp(log_meas_f, log_cal_f, cal_db,
                        left=cal_db[0], right=cal_db[-1])
    return spl_db - interp


def resolve_device(name: str, kind: str) -> int:
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if name.lower() in d["name"].lower():
            if kind == "input" and d["max_input_channels"] > 0:
                return i
            if kind == "output" and d["max_output_channels"] > 0:
                return i
    raise RuntimeError(f"No {kind} device matching {name!r}")


def play_and_capture(sweep: np.ndarray, fs: int,
                      out_dev: int, in_dev: int,
                      pre_silence_s: float = 0.3,
                      post_silence_s: float = 0.5) -> np.ndarray:
    pre = int(pre_silence_s * fs)
    post = int(post_silence_s * fs)
    out_stereo = np.column_stack([sweep, sweep])
    silence_pre = np.zeros((pre, 2), dtype=np.float32)
    silence_post = np.zeros((post, 2), dtype=np.float32)
    out_full = np.vstack([silence_pre, out_stereo, silence_post])
    print(f"  playing {len(out_full) / fs:.2f} s ({out_dev} → {in_dev})...", flush=True)
    capture = sd.playrec(out_full, samplerate=fs,
                          device=(in_dev, out_dev),
                          channels=2,
                          dtype="float32",
                          blocking=True)
    return capture[pre:pre + len(sweep), 0]  # UMIK-1 mono channel


def freq_response(ir: np.ndarray, fs: int,
                   window_ms: float = 300.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    peak_idx = int(np.argmax(np.abs(ir)))
    n_window = int(window_ms * 1e-3 * fs)
    start = max(0, peak_idx - n_window // 8)
    end = min(len(ir), start + n_window)
    windowed = ir[start:end].copy()
    n_fade = int(0.25 * len(windowed))
    tail_fade = 0.5 * (1 + np.cos(np.linspace(0, np.pi, n_fade)))
    windowed[-n_fade:] *= tail_fade
    n_fft = 1 << int(np.ceil(np.log2(len(windowed))))
    H = np.fft.rfft(windowed, n_fft)
    freq = np.fft.rfftfreq(n_fft, 1 / fs)
    mag_db = 20 * np.log10(np.abs(H) + 1e-30)
    phase_deg = np.degrees(np.angle(H))
    return freq, mag_db, phase_deg


def to_log_grid(freq: np.ndarray, mag_db: np.ndarray, phase_deg: np.ndarray,
                 f_lo: float, f_hi: float, ppo: int = 96) -> tuple:
    n_oct = np.log2(f_hi / f_lo)
    n_points = int(n_oct * ppo) + 1
    log_freqs = f_lo * 2 ** (np.arange(n_points) / ppo)
    valid = freq > 0
    log_freq_src = np.log(freq[valid])
    log_freq_dst = np.log(log_freqs)
    mag_interp = np.interp(log_freq_dst, log_freq_src, mag_db[valid])
    phase_interp = np.interp(log_freq_dst, log_freq_src, phase_deg[valid])
    return log_freqs, mag_interp, phase_interp


def write_rew_txt(path: str, title: str, freqs: np.ndarray,
                   spl_db: np.ndarray, phase_deg: np.ndarray,
                   sweep_label: str, again_db: float,
                   input_name: str) -> None:
    date_str = _dt.datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")
    ratio = freqs[1] / freqs[0]
    ppo = int(round(1 / np.log2(ratio)))
    with open(path, "w") as f:
        f.write("* Measurement data measured by dsp408-py tools/measure\n")
        f.write(f"* Source: {input_name}  Gain: {int(again_db)}dB\n")
        f.write(f"* Format: {sweep_label}, 1 sweep at {SWEEP_LEVEL_DBFS:.1f} dBFS\n")
        f.write(f"* Dated: {date_str}\n")
        f.write("* Settings:\n")
        f.write("*  C-weighting compensation: Off\n")
        f.write(f"* Note: ; \n")
        f.write(f"* Measurement: {title}\n")
        f.write("* Smoothing: None\n")
        f.write(f"* Frequency Step: {ppo} ppo\n")
        f.write(f"* Start Frequency: {freqs[0]:.6f} Hz\n")
        f.write("*\n")
        f.write("* Freq(Hz) SPL(dB) Phase(degrees)\n")
        for fr, s, p in zip(freqs, spl_db, phase_deg):
            f.write(f"{fr:.6f} {s:.3f} {p:.4f}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title", required=True, help="Measurement title")
    ap.add_argument("--output", required=True, help="Output .txt file path")
    ap.add_argument("--sweep-length", type=int, default=DEFAULT_SWEEP_LENGTH)
    ap.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    ap.add_argument("--f-lo", type=float, default=20.0)
    ap.add_argument("--f-hi", type=float, default=22000.0)
    ap.add_argument("--cal-file", default=None,
                    help="Mic calibration .txt (UMIK-1 format). Optional.")
    ap.add_argument("--output-device", default=DEFAULT_OUTPUT_DEVICE)
    ap.add_argument("--input-device", default=DEFAULT_INPUT_DEVICE)
    ap.add_argument("--out-freq-lo", type=float, default=20.0)
    ap.add_argument("--out-freq-hi", type=float, default=20000.0)
    ap.add_argument("--ppo", type=int, default=96)
    args = ap.parse_args()

    fs = args.sample_rate
    out_dev = resolve_device(args.output_device, "output")
    in_dev = resolve_device(args.input_device, "input")

    sweep_label = f"{args.sweep_length // 1024}k Log Swept Sine"
    print(f"=== {args.title} ===")
    print(f"  Sweep: {args.sweep_length} samples @ {fs} Hz "
          f"({args.sweep_length / fs:.2f} s), {args.f_lo}-{args.f_hi} Hz")
    print(f"  Output: {sd.query_devices(out_dev)['name']}")
    print(f"  Input:  {sd.query_devices(in_dev)['name']}")

    sweep = log_sweep(args.sweep_length, fs, args.f_lo, args.f_hi,
                       level_dbfs=SWEEP_LEVEL_DBFS)
    capture = play_and_capture(sweep, fs, out_dev, in_dev)
    peak_dbfs = 20 * np.log10(np.max(np.abs(capture)) + 1e-30)
    print(f"  mic capture peak: {peak_dbfs:.1f} dBFS")
    if peak_dbfs > -3:
        print("  WARNING: capture close to clipping")
    if peak_dbfs < -40:
        print("  WARNING: capture very quiet — check audio chain")

    ir = deconvolve(capture, sweep)
    freq_full, mag_db_full, phase_full = freq_response(ir, fs, window_ms=300.0)
    log_freqs, mag_log, phase_log = to_log_grid(
        freq_full, mag_db_full, phase_full,
        args.out_freq_lo, args.out_freq_hi, ppo=args.ppo)

    again_db = DEFAULT_AGAIN_DB
    sens_factor_db = DEFAULT_SENS_FACTOR_DB
    if args.cal_file:
        cal_f, cal_db, meta = load_mic_cal(args.cal_file)
        again_db = meta.get("again_db", again_db)
        sens_factor_db = meta.get("sens_factor_db", sens_factor_db)
        spl_db = mag_log + again_db + sens_factor_db
        spl_db = apply_mic_cal(log_freqs, spl_db, cal_f, cal_db)
    else:
        spl_db = mag_log + again_db + sens_factor_db

    write_rew_txt(args.output, args.title, log_freqs, spl_db, phase_log,
                   sweep_label, again_db, sd.query_devices(in_dev)["name"])
    print(f"  saved → {args.output}")


if __name__ == "__main__":
    main()
