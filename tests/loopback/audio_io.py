"""Audio I/O helpers for DSP-408 loopback testing via the Scarlett 2i2 4th Gen.

The Scarlett 2i2 4th Gen exposes:
  - 2 playback channels (OUT 1, OUT 2)
  - 4 capture channels:
        ch1 = IN 1 (mic/line/instrument)
        ch2 = IN 2
        ch3 = internal loopback of OUT 1 (what's being played)
        ch4 = internal loopback of OUT 2

So a loopback test needs no cables: just play through OUT 1+2 and record
ch3+4 on capture. That's how this module's `selfloop_*` helpers work.

For DSP-408 testing, wire:
    Scarlett OUT 1 → DSP IN 1
    Scarlett OUT 2 → DSP IN 2
    DSP OUT 1     → Scarlett IN 1 (capture ch1)
    DSP OUT 2     → Scarlett IN 2 (capture ch2)

Then `dsploop_*` helpers play→DSP→capture in one pass and return the
recorded signal aligned to the playback timeline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

# ── Hardware constants for Scarlett 2i2 4th Gen ───────────────────────────
SCARLETT_NAME_HINT = "Scarlett"
DEFAULT_SR = 48000           # Scarlett accepts 44100..192000
DEFAULT_FORMAT = "int32"     # S32_LE — only format the Gen 4 supports
PLAYBACK_CHANNELS = 2        # OUT 1, OUT 2
CAPTURE_CHANNELS = 4         # IN 1, IN 2, loopback OUT 1, loopback OUT 2

# Amplitude full-scale for int32
INT32_MAX = 0x7FFFFFFF


def find_scarlett() -> int:
    """Return the sounddevice device index for the Scarlett."""
    for i, dev in enumerate(sd.query_devices()):
        if SCARLETT_NAME_HINT.lower() in dev["name"].lower():
            return i
    raise RuntimeError(
        f"No device matching {SCARLETT_NAME_HINT!r} found. "
        f"Available: {[d['name'] for d in sd.query_devices()]}"
    )


# ── Signal generation ────────────────────────────────────────────────────
def sine(freq_hz: float, duration_s: float, amp_dbfs: float = -20.0,
         sr: int = DEFAULT_SR) -> np.ndarray:
    """Generate a mono float64 sine wave normalised to [-1, +1]."""
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    amp = 10 ** (amp_dbfs / 20.0)
    return amp * np.sin(2 * np.pi * freq_hz * t)


def silence(duration_s: float, sr: int = DEFAULT_SR) -> np.ndarray:
    return np.zeros(int(duration_s * sr), dtype=np.float64)


def log_sweep(f_start: float, f_end: float, duration_s: float,
              amp_dbfs: float = -20.0, sr: int = DEFAULT_SR) -> np.ndarray:
    """Logarithmic sine sweep — Farina-style, useful for filter measurement.

    Returns a mono float64 chirp from f_start → f_end over duration_s.
    """
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    L = duration_s / math.log(f_end / f_start)
    amp = 10 ** (amp_dbfs / 20.0)
    return amp * np.sin(2 * np.pi * f_start * L * (np.exp(t / L) - 1))


# ── Float ↔ int32 conversion ─────────────────────────────────────────────
def float_to_int32(x: np.ndarray) -> np.ndarray:
    """Float [-1, +1] → int32 [-INT32_MAX, +INT32_MAX], clipped at boundaries."""
    return np.clip(x * INT32_MAX, -INT32_MAX, INT32_MAX).astype(np.int32)


def int32_to_float(x: np.ndarray) -> np.ndarray:
    return x.astype(np.float64) / INT32_MAX


# ── Mono → multi-channel layout helpers ──────────────────────────────────
def mono_to_stereo(x: np.ndarray, left: bool = True, right: bool = True) -> np.ndarray:
    """Place a mono signal on one or both Scarlett OUT channels."""
    out = np.zeros((len(x), PLAYBACK_CHANNELS), dtype=np.float64)
    if left:
        out[:, 0] = x
    if right:
        out[:, 1] = x
    return out


# ── Synchronous play+record (the workhorse) ──────────────────────────────
@dataclass
class CaptureBlock:
    """Result of one play+record pass.

    Each channel is a float64 numpy array, normalised to [-1, +1].
    Index = `samples`, value = sample.

    For Scarlett 4th Gen capture:
      in1, in2 = analog mic/line inputs (= DSP outputs in our rig)
      lp1, lp2 = internal loopback of what we just played (sanity reference)
    """
    sr: int
    in1: np.ndarray
    in2: np.ndarray
    lp1: np.ndarray
    lp2: np.ndarray

    def __len__(self) -> int:
        return len(self.in1)


def play_and_record(playback_lr: np.ndarray, sr: int = DEFAULT_SR,
                    device: int | None = None,
                    pad_pre_s: float = 0.05,
                    pad_post_s: float = 0.05) -> CaptureBlock:
    """Play a 2-channel signal and simultaneously capture all 4 inputs.

    Args:
        playback_lr: shape (n, 2) float64 in [-1, +1].
        sr: sample rate (must match what device is configured for).
        device: sounddevice index; auto-detect Scarlett if None.
        pad_pre_s: silence prepended to the playback so capture has a
            stable lead-in (filters startup glitches).
        pad_post_s: silence appended; gives delay tails room to settle.

    Returns:
        CaptureBlock with all 4 capture channels (float64).
    """
    if device is None:
        device = find_scarlett()
    pre = np.zeros((int(pad_pre_s * sr), PLAYBACK_CHANNELS), dtype=np.float64)
    post = np.zeros((int(pad_post_s * sr), PLAYBACK_CHANNELS), dtype=np.float64)
    full_play = np.vstack([pre, playback_lr, post])

    # sounddevice plays back float — we convert to int32 manually to bit-perfect
    # match the Scarlett's S32_LE format and avoid hidden dithering / scaling.
    playback_int32 = float_to_int32(full_play)

    captured = sd.playrec(
        playback_int32,
        samplerate=sr,
        channels=CAPTURE_CHANNELS,
        dtype=DEFAULT_FORMAT,
        device=device,
        blocking=True,
    )
    captured_f = int32_to_float(captured)
    return CaptureBlock(
        sr=sr,
        in1=captured_f[:, 0],
        in2=captured_f[:, 1],
        lp1=captured_f[:, 2],
        lp2=captured_f[:, 3],
    )


# ── Measurements ─────────────────────────────────────────────────────────
def rms_dbfs(x: np.ndarray) -> float:
    """RMS level in dBFS. Returns -inf for pure silence."""
    rms = math.sqrt(float(np.mean(x.astype(np.float64) ** 2)))
    return 20 * math.log10(rms) if rms > 0 else float("-inf")


def peak_dbfs(x: np.ndarray) -> float:
    p = float(np.max(np.abs(x)))
    return 20 * math.log10(p) if p > 0 else float("-inf")


def tone_level_at(x: np.ndarray, sr: int, freq_hz: float,
                  bw_hz: float = 5.0) -> float:
    """Measure the level (in dBFS) of a single tone at `freq_hz`.

    Uses an FFT bin-sum within ±bw_hz of the target frequency. Robust to
    leakage and guard-band noise.
    """
    n = len(x)
    if n == 0:
        return float("-inf")
    # Hann-windowed FFT
    win = np.hanning(n)
    spec = np.fft.rfft(x * win)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    bin_mask = np.abs(freqs - freq_hz) <= bw_hz
    if not bin_mask.any():
        return float("-inf")
    # window correction (Hann coherent gain = 0.5)
    bin_power = (np.abs(spec[bin_mask]) ** 2).sum() * (2.0 / n / 0.5) ** 2
    rms = math.sqrt(bin_power / 2.0)  # /2 for sine RMS = peak/sqrt(2)
    return 20 * math.log10(rms) if rms > 0 else float("-inf")


def transfer_function(input_signal: np.ndarray, output_signal: np.ndarray,
                      sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Estimate magnitude transfer function output/input via FFT division.

    Useful for sweep measurements — feed an exponential chirp as input,
    capture the system output, and this returns (frequencies, gain_dB).
    """
    n = min(len(input_signal), len(output_signal))
    win = np.hanning(n)
    in_spec = np.fft.rfft(input_signal[:n] * win)
    out_spec = np.fft.rfft(output_signal[:n] * win)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    # Avoid divide-by-near-zero
    eps = np.max(np.abs(in_spec)) * 1e-9
    gain = np.where(np.abs(in_spec) > eps,
                    np.abs(out_spec) / np.abs(in_spec),
                    0.0)
    gain_db = 20 * np.log10(np.maximum(gain, 1e-12))
    return freqs, gain_db


# ── Self-test (no cables, no DSP — just round-trip via Scarlett loopback) ─
def selfloop_sanity(freq_hz: float = 1000.0, amp_dbfs: float = -20.0,
                    duration_s: float = 0.5) -> None:
    """Verify the Scarlett rig works end-to-end without any external wiring.

    Plays a tone via OUT 1+2 and reads back via the device's internal
    loopback channels (capture ch3, ch4). Prints measured levels.

    Expected (no DSP, no cables):
        IN 1, IN 2: ambient noise floor (-90..-60 dBFS depending on gain)
        loopback 1, 2: should match the played signal level (~-20 dBFS)
    """
    print(f"[selfloop] device: {find_scarlett()}")
    tone = sine(freq_hz, duration_s, amp_dbfs=amp_dbfs)
    play = mono_to_stereo(tone)
    cap = play_and_record(play)
    n_lead = int(0.05 * cap.sr)
    n_tail = int(0.05 * cap.sr)
    body = slice(n_lead, len(cap) - n_tail)

    print(f"[selfloop] played  {freq_hz} Hz @ {amp_dbfs:+.1f} dBFS for {duration_s} s")
    for label, sig in [("IN 1     ", cap.in1[body]),
                       ("IN 2     ", cap.in2[body]),
                       ("loopback1", cap.lp1[body]),
                       ("loopback2", cap.lp2[body])]:
        print(f"  {label}: rms={rms_dbfs(sig):+7.1f} dBFS  "
              f"peak={peak_dbfs(sig):+7.1f} dBFS  "
              f"tone@{int(freq_hz)}Hz={tone_level_at(sig, cap.sr, freq_hz):+7.1f} dBFS")


if __name__ == "__main__":
    selfloop_sanity()
