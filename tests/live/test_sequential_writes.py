"""Regression: N sequential writes must ALL land — the firmware does
not silently stop accepting writes after some threshold.

This was originally reported as "writes stop landing after ~5 in a
session", but the evidence was read-divergence artifacts (see
test_read_stability.py).  With the library's default double-read
fixed, sequential-write tests must pass.
"""
from __future__ import annotations

import struct
import time

import pytest


@pytest.mark.parametrize("n", [20])
def test_sequential_set_channel(dsp, n):
    """N sequential set_channel() writes to channel 3 all land."""
    failures = []
    try:
        for i in range(n):
            want_db = -0.5 * (i + 1)  # -0.5, -1.0, ..., -10.0
            dsp.set_channel(3, db=want_db, muted=False)
            time.sleep(0.05)
            blob = dsp.read_channel_state(3)
            raw = struct.unpack("<H", blob[248:250])[0]
            actual_db = (raw - 600) / 10.0
            if abs(actual_db - want_db) >= 0.05:
                failures.append((i, want_db, actual_db))
    finally:
        dsp.set_channel(3, db=0.0, muted=False)
    assert not failures, f"writes failed at iterations {failures}"


@pytest.mark.parametrize("n", [20])
def test_sequential_set_eq_band(dsp, n):
    """N sequential set_eq_band() writes to ch2 band 5 all land.

    (Band 5 rather than 0 because we independently also assert in
    test_surgical_writes that band 0 works.)
    """
    failures = []
    try:
        for i in range(n):
            want_f = 500 + i * 13     # 500, 513, 526, ...
            want_bw = 40 + i          # distinct bw per iteration
            dsp.set_eq_band(channel=2, band=5, freq_hz=want_f,
                            gain_db=0.0, bandwidth_byte=want_bw)
            time.sleep(0.05)
            blob = dsp.read_channel_state(2)
            actual_f = struct.unpack("<H", blob[40:42])[0]
            actual_bw = blob[44]
            if actual_f != want_f or actual_bw != want_bw:
                failures.append((i, (want_f, want_bw), (actual_f, actual_bw)))
    finally:
        dsp.set_eq_band(channel=2, band=5, freq_hz=1000, gain_db=0.0,
                        bandwidth_byte=0x34)
    assert not failures, f"eq_band writes failed at iterations {failures}"


def test_sequential_set_channel_no_sleep(dsp):
    """Back-to-back writes with NO artificial delay — tests whether USB
    or firmware needs pacing to keep up.  If this fails, we need
    transport-level rate limiting."""
    try:
        want_values = [-1.0, -2.0, -3.0, -4.0, -5.0,
                       -6.0, -7.0, -8.0, -9.0, -10.0]
        for v in want_values:
            dsp.set_channel(3, db=v, muted=False)
        # Settle, then check the LAST write landed
        time.sleep(0.2)
        blob = dsp.read_channel_state(3)
        raw = struct.unpack("<H", blob[248:250])[0]
        actual_db = (raw - 600) / 10.0
        assert abs(actual_db - (-10.0)) < 0.05, (
            f"last of 10 no-sleep writes didn't land: got {actual_db}"
        )
    finally:
        dsp.set_channel(3, db=0.0, muted=False)
