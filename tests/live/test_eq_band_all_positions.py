"""Regression for the once-suspected "Bug 1":
``set_eq_band(ch>0, band=0)`` was reported as a silent no-op.  It
isn't — it works on every (channel, band=0..5) combination.  This test
writes all 48 (8 channels × 6 bands) positions in one session with
distinctive values and asserts every one round-trips via the
``blob[band*8 .. band*8+4]`` layout.

Bands 6..9 are EXCLUDED: the firmware stores them at a different
offset/stride than bands 0..5 — writes do land but don't appear at the
``band*8`` offset (see ``notes/blob-layout-verification.md``).  Until
we fully decode bands 6..9, those positions are covered only by the
higher-level surgical tests (which don't assume a specific offset).

This test also stress-tests the sequential-write path (48 writes in a
single session must all land — the "writes stop after ~5" bug report
would fail this test immediately).
"""
from __future__ import annotations

import struct
import time

N_BANDS_VERIFIED_LAYOUT = 6   # bands 0..5


def test_all_eq_verified_positions_round_trip(dsp):
    failures = []
    try:
        # Write distinctive values to every (ch, band) pair in the
        # verified-layout range
        targets: dict[tuple[int, int], tuple[int, int, int]] = {}
        for ch in range(8):
            for band in range(N_BANDS_VERIFIED_LAYOUT):
                f = 100 + ch * 32 + band      # unique per (ch, band)
                # gain_raw = 610 + ch * 7 + band * 3 → gain_db = (raw - 600) / 10
                gain_raw = 610 + ch * 7 + band * 3
                gain_db = (gain_raw - 600) / 10.0
                bw = 40 + ch + band * 2
                targets[(ch, band)] = (f, gain_raw, bw)
                dsp.set_eq_band(ch, band, freq_hz=f, gain_db=gain_db,
                                bandwidth_byte=bw)
                # tiny sleep so we don't overrun USB transaction latency
                time.sleep(0.02)

        # Settle & verify via readback
        time.sleep(0.3)
        for ch in range(8):
            blob = dsp.read_channel_state(ch)
            for band in range(N_BANDS_VERIFIED_LAYOUT):
                want_f, want_g, want_bw = targets[(ch, band)]
                off = band * 8
                got_f = struct.unpack("<H", blob[off:off + 2])[0]
                got_g = struct.unpack("<H", blob[off + 2:off + 4])[0]
                got_bw = blob[off + 4]
                if (got_f, got_g, got_bw) != (want_f, want_g, want_bw):
                    failures.append(
                        (ch, band,
                         (want_f, want_g, want_bw),
                         (got_f, got_g, got_bw))
                    )
    finally:
        # Restore every band to its factory default (verified-layout
        # bands only — bands 6..9 are not re-written here since we
        # didn't perturb them)
        for ch in range(8):
            for band in range(N_BANDS_VERIFIED_LAYOUT):
                dsp.set_eq_band(ch, band,
                                freq_hz=dsp.EQ_DEFAULT_FREQS_HZ[band],
                                gain_db=0.0, bandwidth_byte=0x34)
                time.sleep(0.01)

    assert not failures, (
        f"{len(failures)}/{8 * N_BANDS_VERIFIED_LAYOUT} EQ band writes "
        f"did not round-trip:\n" +
        "\n".join(
            f"  ch{ch} band{b}: wanted {w}, got {g}"
            for ch, b, w, g in failures[:20]
        )
    )
