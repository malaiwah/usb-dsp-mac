"""Persistence-across-session regression: every mutating public API's
write must survive a ``Device.close()`` + reopen within the same USB
power cycle.

The originally-reported "RAM-only, wipes across sessions" bug did NOT
reproduce on our office firmware (v1.06 ``MYDW-AV1.06``).  Cross-
POWER-CYCLE persistence (yank USB, plug back in) still requires
``save_preset()`` and is harder to automate — we verify it manually;
see tests/live/README.md.

These tests deliberately DON'T use the module-scoped ``dsp`` fixture —
they open, write, close, reopen, verify.  Each test restores factory
defaults at the end by opening a third time, so nothing leaks across
runs.
"""
from __future__ import annotations

import os
import struct
from contextlib import contextmanager

import pytest


def _open_device():
    from dsp408 import Device, enumerate_devices
    devs = enumerate_devices()
    if not devs:
        pytest.skip("no DSP-408 enumerable")
    selector = os.environ.get("DSP408_DEVICE")
    if selector:
        return Device.open(selector=selector)
    return Device.open(path=devs[0]["path"])


@contextmanager
def _session():
    """Open + connect + close as a context."""
    d = _open_device()
    d.connect()
    try:
        yield d
    finally:
        try:
            d.close()
        except Exception:
            pass


# ── set_channel ────────────────────────────────────────────────────────
def test_set_channel_gain_persists_across_reopen():
    want_db = -7.5
    try:
        with _session() as d:
            d.set_channel(4, db=want_db, muted=False)
        with _session() as d:
            blob = d.read_channel_state(4)
            raw = struct.unpack("<H", blob[248:250])[0]
            after_db = (raw - 600) / 10.0
        assert abs(after_db - want_db) < 0.05, (
            f"gain did not persist: wrote {want_db} dB, reopened and "
            f"read {after_db} dB.  If intermittent, check for auto-load-"
            f"preset-on-open firmware behavior."
        )
    finally:
        with _session() as d:
            d.set_channel(4, db=0.0, muted=False)


def test_set_channel_mute_persists_across_reopen():
    try:
        with _session() as d:
            d.set_channel(5, db=0.0, muted=True)
        with _session() as d:
            blob = d.read_channel_state(5)
            muted = blob[246] == 0   # blob[246]: 1 audible, 0 muted
        assert muted, "mute state did not persist across reopen"
    finally:
        with _session() as d:
            d.set_channel(5, db=0.0, muted=False)


# ── set_routing ────────────────────────────────────────────────────────
def test_set_routing_persists_across_reopen():
    try:
        with _session() as d:
            d.set_routing(6, in1=True, in2=False, in3=True, in4=False)
        with _session() as d:
            blob = d.read_channel_state(6)
            routing = list(blob[262:266])
        assert routing == [100, 0, 100, 0], (
            f"routing did not persist: wrote [100, 0, 100, 0], "
            f"read {routing}"
        )
    finally:
        with _session() as d:
            d.set_routing(6, False, False, False, False)


# ── set_crossover ──────────────────────────────────────────────────────
def test_set_crossover_persists_across_reopen():
    try:
        with _session() as d:
            d.set_crossover(
                2,
                hpf_freq=123, hpf_filter=2, hpf_slope=3,
                lpf_freq=17890, lpf_filter=2, lpf_slope=3,
            )
        with _session() as d:
            blob = d.read_channel_state(2)
            hpf_f = struct.unpack("<H", blob[254:256])[0]
            hpf_filter = blob[256]
            hpf_slope = blob[257]
            lpf_f = struct.unpack("<H", blob[258:260])[0]
            lpf_filter = blob[260]
            lpf_slope = blob[261]
        assert (hpf_f, hpf_filter, hpf_slope,
                lpf_f, lpf_filter, lpf_slope) == \
               (123, 2, 3, 17890, 2, 3), (
            "crossover did not persist across reopen"
        )
    finally:
        with _session() as d:
            d.set_crossover(
                2,
                hpf_freq=20, hpf_filter=0, hpf_slope=1,
                lpf_freq=20000, lpf_filter=0, lpf_slope=1,
            )


# ── set_eq_band ────────────────────────────────────────────────────────
def test_set_eq_band_persists_across_reopen():
    want_f, want_bw = 700, 70
    try:
        with _session() as d:
            d.set_eq_band(channel=2, band=3, freq_hz=want_f,
                          gain_db=0.0, bandwidth_byte=want_bw)
        with _session() as d:
            blob = d.read_channel_state(2)
            # band 3 at offset 24..27 (freq), 28 (bw)
            actual_f = struct.unpack("<H", blob[24:26])[0]
            actual_bw = blob[28]
        assert (actual_f, actual_bw) == (want_f, want_bw), (
            f"EQ band did not persist: wrote f={want_f} bw={want_bw}, "
            f"read f={actual_f} bw={actual_bw}"
        )
    finally:
        with _session() as d:
            d.set_eq_band(channel=2, band=3, freq_hz=250,
                          gain_db=0.0, bandwidth_byte=0x34)


# ── set_channel_name ──────────────────────────────────────────────────
def test_set_channel_name_persists_across_reopen():
    want_name = "MIDLEFT!"
    try:
        with _session() as d:
            d.set_channel_name(3, want_name)
        with _session() as d:
            blob = d.read_channel_state(3)
            got_name = blob[286:294].decode("ascii", errors="replace")
        assert got_name == want_name, (
            f"channel name did not persist: wrote {want_name!r}, "
            f"read {got_name!r}"
        )
    finally:
        with _session() as d:
            d.set_channel_name(3, "        ")


# ── set_master ─────────────────────────────────────────────────────────
def test_set_master_gain_persists_across_reopen():
    """The master gain lives in a different register from per-channel
    blobs — persistence is via the preset subsystem.  We assert
    round-trip-via-read behavior is consistent across reopen."""
    try:
        with _session() as d:
            d.set_master(db=-9.0, muted=False)
        with _session() as d:
            # get_master returns (db, muted) tuple
            after_db, _muted = d.get_master()
        assert abs(after_db - (-9.0)) < 0.5, (
            f"master gain did not persist: wrote -9.0 dB, "
            f"read {after_db} dB"
        )
    finally:
        with _session() as d:
            d.set_master(db=0.0, muted=False)


# ── save_preset persistence contract ──────────────────────────────────
# (Not implemented here: would corrupt the saved preset slot.
# See README.md for manual verification procedure.)
