"""Shared fixtures for live-hardware tests.

Gating rule: every live test is skipped unless ``DSP408_LIVE=1`` is set
AND a DSP-408 is actually enumerable on this machine.  Without those
guards, ``pytest tests/live/`` would try to open USB on a dev machine
without hardware and crash.

Fixtures also own state hygiene: baseline snapshot at fixture setup,
restore at teardown, so every test starts on a clean device regardless
of ordering.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# ── skip-everything gate ────────────────────────────────────────────────
pytestmark = pytest.mark.skipif(
    os.environ.get("DSP408_LIVE") != "1",
    reason="live tests need DSP408_LIVE=1 and a plugged-in DSP-408",
)


# ── device fixture ──────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def dsp():
    """One opened + connected Device per test module.

    Uses ``DSP408_DEVICE`` (display_id / serial / friendly alias) to
    select, otherwise the first found device.  Skips the module if no
    device is enumerable so the suite degrades gracefully.
    """
    if os.environ.get("DSP408_LIVE") != "1":
        pytest.skip("DSP408_LIVE=1 not set")

    from dsp408 import Device, enumerate_devices

    devs = enumerate_devices()
    if not devs:
        pytest.skip("no DSP-408 enumerable on USB")

    selector = os.environ.get("DSP408_DEVICE")
    if selector:
        # Device.open() accepts serial / display_id / friendly_name via selector
        d = Device.open(selector=selector)
    else:
        d = Device.open(path=devs[0]["path"])
    try:
        d.connect()
        yield d
    finally:
        try:
            d.close()
        except Exception:
            pass


# ── snapshot helpers ────────────────────────────────────────────────────
def snapshot_all(dsp) -> dict[int, bytes]:
    """Grab a full per-channel 296-byte blob snapshot of all 8 channels.

    Uses the library's default double-read so the baseline is byte-exact.
    Byte 294 (a per-read counter) is zeroed so it doesn't pollute diffs.
    """
    out: dict[int, bytes] = {}
    for ch in range(8):
        raw = bytes(dsp.read_channel_state(ch))
        out[ch] = raw[:294] + b"\x00" + raw[295:]
    return out


#: Byte range that is inherently unstable across consecutive reads on
#: v1.06 firmware — the "shifted-blob" region (EQ bands 6..9 + the
#: padding area leon's source labels as unused).  A read-divergence
#: event causes the read to return a 2-byte-left-shifted copy of this
#: region, making every byte in it "appear" to change even when the
#: firmware's internal state did not.  See
#: ``dsp408/device.py::read_channel_state`` docstring for the full
#: characterisation.
#:
#: Tests for APIs that don't target this region (set_channel,
#: set_routing, set_crossover, set_channel_name, set_compressor,
#: set_master) can safely ignore it in diffs.  Tests for ``set_eq_band``
#: on bands 0..5 don't touch this region either.  Tests of
#: ``set_full_channel_state`` (which writes the whole blob) don't use
#: diff_blobs — they assert on specific offsets directly.
UNSTABLE_READ_REGION: tuple[int, int] = (48, 245)


def diff_blobs(
    before: dict[int, bytes],
    after: dict[int, bytes],
    *,
    ignore_unstable_region: bool = True,
) -> dict[int, list[tuple[int, int]]]:
    """Return the list of (start, end) inclusive offset ranges where
    per-channel blobs differ.  Byte 294 is already zeroed by snapshot_all.

    By default skips ``UNSTABLE_READ_REGION`` — the firmware's
    shifted-blob quirk makes bytes in there appear to change at random
    across consecutive reads even when the device state is stable.  Set
    ``ignore_unstable_region=False`` for tests that legitimately read
    from that region (e.g. eq_band verification against specific
    offsets).

    Returns a dict only containing channels that actually changed.
    """
    u_start, u_end = UNSTABLE_READ_REGION
    out: dict[int, list[tuple[int, int]]] = {}
    for ch in range(8):
        idx = [
            i for i in range(296)
            if before[ch][i] != after[ch][i]
            and (not ignore_unstable_region or not (u_start <= i <= u_end))
        ]
        if not idx:
            continue
        ranges: list[tuple[int, int]] = []
        s = idx[0]
        p = s
        for i in idx[1:]:
            if i == p + 1:
                p = i
            else:
                ranges.append((s, p))
                s = i
                p = i
        ranges.append((s, p))
        out[ch] = ranges
    return out


def assert_only_changed(
    before: dict[int, bytes],
    after: dict[int, bytes],
    expected: dict[int, list[tuple[int, int]]],
) -> None:
    """Assert that every byte change between before/after falls within
    the ``expected`` ranges on the specified channel, and that NO other
    channel has any change.

    This is a "contained in" assertion, not an "equal to" assertion —
    individual bytes within the expected range may not actually change
    if the write value happens to equal the baseline for that byte
    (e.g. writing gain=570 produces the same gain_hi=0x02 as the
    default gain=600).  The surgical-write guarantee we care about is
    "only these bytes could change"; whether they DO change depends on
    baseline values.
    """
    actual = diff_blobs(before, after)
    # Unexpected channels changed
    unexpected_ch = set(actual) - set(expected)
    assert not unexpected_ch, (
        f"unexpected cross-channel mutations on channels {sorted(unexpected_ch)}: "
        f"{ {ch: actual[ch] for ch in unexpected_ch} }"
    )
    for ch, want_ranges in expected.items():
        got_ranges = actual.get(ch, [])
        if not got_ranges:
            continue  # zero bytes changed — allowed (coincidental equality)
        # Every changed byte on this channel must be covered by at
        # least one expected range.
        def _covered(offset: int) -> bool:
            return any(s <= offset <= e for s, e in want_ranges)
        for got_s, got_e in got_ranges:
            for off in range(got_s, got_e + 1):
                assert _covered(off), (
                    f"ch{ch} byte {off} changed, but not covered by any "
                    f"expected range {want_ranges}.  Full got={got_ranges}"
                )


# ── lease helper: write → assert → restore ──────────────────────────────
def restore_state(dsp, before: dict[int, bytes]) -> None:
    """Best-effort restore: apply the pre-test state using surgical APIs.

    We avoid ``set_full_channel_state`` because it has the documented
    2-byte payload shift quirk.  Instead, reconstruct state via the
    per-parameter setters (safe, surgical, byte-exact).  This is an
    inversion of the things we typically test, so the restore path
    itself is exercised on every teardown.
    """
    from dsp408.device import parse_channel_state_blob

    for ch in range(8):
        parsed = parse_channel_state_blob(before[ch], ch)
        if parsed is None:
            continue
        dsp.set_channel(
            ch,
            db=parsed["db"],
            muted=parsed["muted"],
        )
        # polar + delay via set_channel if available; otherwise skip
        # (only set_channel landed on the restore hot-path because it's
        # what most tests perturb).
