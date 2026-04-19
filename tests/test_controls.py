"""Verify the high-level control APIs (master / per-channel / routing)
produce byte-for-byte the same HID frames the Windows GUI emits.

Each "expected payload" is copied from
`captures/full-sequence.pcapng`, so a passing test here means our
control plane is bit-compatible with DSP-408.exe V1.24.
"""
from __future__ import annotations

from collections import deque

import pytest

from dsp408.device import Device
from dsp408.protocol import (
    CAT_PARAM,
    CAT_STATE,
    CMD_MASTER,
    DIR_WRITE,
    DIR_WRITE_ACK,
    Frame,
    parse_frame,
)


# ── fake transport that captures frames ──────────────────────────────────
class FakeTransport:
    """Stand-in for dsp408.transport.Transport that just records frames
    and returns canned write-ack replies, so we can drive the high-level
    Device API without USB hardware."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._queued_replies: deque[Frame] = deque()
        self.hid = self  # the close()-target

    # methods Device._exchange uses
    def send_frame(self, frame: bytes) -> None:
        self.sent.append(frame)

    def read_response(self, timeout_ms: int = 2000) -> Frame | None:
        if self._queued_replies:
            return self._queued_replies.popleft()
        # Auto-ack every write so set_*() returns cleanly.
        last = self.sent[-1] if self.sent else b""
        f = parse_frame(last)
        if f is None:
            return None
        # Build a write-ack mirror of the request
        from dsp408.protocol import build_frame
        ack_raw = build_frame(direction=DIR_WRITE_ACK, seq=f.seq,
                              cmd=f.cmd, data=b"", category=f.category)
        return parse_frame(ack_raw)

    # Device.close() calls hid.close()
    def close(self) -> None:
        pass

    def queue_reply(self, frame: Frame) -> None:
        self._queued_replies.append(frame)


def _make_device() -> tuple[Device, FakeTransport]:
    """Build a Device backed by FakeTransport (no USB)."""
    t = FakeTransport()
    d = Device(t, info={"display_id": "fake", "path": b"/fake"})
    return d, t


def _last_payload(t: FakeTransport) -> bytes:
    """Return just the 8-byte payload of the last frame sent."""
    f = parse_frame(t.sent[-1])
    return bytes(f.payload[: f.payload_len])


def _last_meta(t: FakeTransport) -> tuple[int, int, int, int]:
    """Return (cmd, cat, dir, seq) of the last frame sent."""
    f = parse_frame(t.sent[-1])
    return f.cmd, f.category, f.direction, f.seq


# ── routing matrix ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "out_idx,ins,expected_cmd,expected_payload_hex",
    [
        # f1325 t=30.75: CH1 routing — IN1 only
        (0, (True, False, False, False), 0x2100, "64 00 00 00 00 00 00 00"),
        # f4377 t=55.49: CH1 routing — all four inputs ON
        (0, (True, True, True, True), 0x2100, "64 64 64 64 00 00 00 00"),
        # f5517 t=64.40: CH2 routing — IN1 only
        (1, (True, False, False, False), 0x2101, "64 00 00 00 00 00 00 00"),
        # f8569 t=88.38: CH1 routing peeled to IN1 only
        (0, (True, False, False, False), 0x2100, "64 00 00 00 00 00 00 00"),
        # f10741 t=105.49: CH4 routing — IN2 only
        (3, (False, True, False, False), 0x2103, "00 64 00 00 00 00 00 00"),
    ],
)
def test_set_routing_matches_capture(out_idx, ins, expected_cmd,
                                     expected_payload_hex) -> None:
    d, t = _make_device()
    d.set_routing(out_idx, *ins)
    cmd, cat, direction, seq = _last_meta(t)
    assert cmd == expected_cmd
    assert cat == CAT_PARAM
    assert direction == DIR_WRITE
    assert seq == 0  # WRITES use seq=0 (matches GUI)
    expected = bytes.fromhex(expected_payload_hex.replace(" ", ""))
    assert _last_payload(t) == expected


@pytest.mark.parametrize(
    "out_idx,levels,expected_cmd,expected_payload_hex",
    [
        # 0x32 = 50% (= -6 dB) on IN1 only
        (0, [0x32, 0, 0, 0], 0x2100, "32 00 00 00 00 00 00 00"),
        # Mixed levels across all 4 inputs
        (3, [100, 50, 25, 200], 0x2103, "64 32 19 c8 00 00 00 00"),
        # Max u8 = +8 dB boost
        (1, [0xFF, 0xFF, 0, 0], 0x2101, "ff ff 00 00 00 00 00 00"),
    ],
)
def test_set_routing_levels_arbitrary_u8(out_idx, levels, expected_cmd,
                                          expected_payload_hex) -> None:
    """Verify set_routing_levels writes arbitrary u8 levels per cell.

    Empirically validated: cell value is a linear amplitude scalar; values
    above 100 boost the signal up to +8 dB at 0xFF (see
    tests/loopback/test_routing_percentage.py for the live measurement).
    """
    d, t = _make_device()
    d.set_routing_levels(out_idx, levels)
    cmd, cat, direction, seq = _last_meta(t)
    assert cmd == expected_cmd
    assert cat == CAT_PARAM
    assert direction == DIR_WRITE
    assert seq == 0
    expected = bytes.fromhex(expected_payload_hex.replace(" ", ""))
    assert _last_payload(t) == expected


def test_set_routing_levels_rejects_bad_args() -> None:
    d, _ = _make_device()
    with pytest.raises(ValueError, match="output_idx"):
        d.set_routing_levels(8, [0, 0, 0, 0])
    with pytest.raises(ValueError, match="must have 4"):
        d.set_routing_levels(0, [100, 100, 100])
    with pytest.raises(ValueError, match="out of u8 range"):
        d.set_routing_levels(0, [256, 0, 0, 0])
    with pytest.raises(ValueError, match="out of u8 range"):
        d.set_routing_levels(0, [-1, 0, 0, 0])


def test_set_routing_bool_calls_set_routing_levels() -> None:
    """The bool wrapper should produce the same wire bytes as a direct
    set_routing_levels call with the same intent."""
    d1, t1 = _make_device()
    d1.set_routing(0, in1=True, in2=False, in3=True, in4=False)
    d2, t2 = _make_device()
    d2.set_routing_levels(0, [100, 0, 100, 0])
    assert _last_payload(t1) == _last_payload(t2)


# ── per-channel volume + mute ───────────────────────────────────────────
@pytest.mark.parametrize(
    "channel,db,muted,expected_cmd,expected_payload_hex",
    [
        # f19565 t=176.04: CH1 vol back to 0 dB (raw 600 = 0x0258)
        (0, 0.0, False, 0x1F00, "01 00 58 02 00 00 00 01"),
        # f13129 t=124.12: CH1 "volume off" — vol=0 (=-60 dB), enabled
        (0, -60.0, False, 0x1F00, "01 00 00 00 00 00 00 01"),
        # CH2 dragged to -30 dB (raw 300 = 0x012c) — final of f14097
        (1, -30.0, False, 0x1F01, "01 00 2c 01 00 00 00 02"),
        # f18789 t=169.41: CH1 muted, vol=0 (mute click after volume-off)
        (0, -60.0, True, 0x1F00, "00 00 00 00 00 00 00 01"),
        # f22037 t=195.43: CH1 muted, vol restored to 600 (en=0, vol=600)
        (0, 0.0, True, 0x1F00, "00 00 58 02 00 00 00 01"),
    ],
)
def test_set_channel_matches_capture(channel, db, muted, expected_cmd,
                                     expected_payload_hex) -> None:
    d, t = _make_device()
    d.set_channel(channel, db=db, muted=muted)
    cmd, cat, direction, seq = _last_meta(t)
    assert cmd == expected_cmd
    assert cat == CAT_PARAM
    assert direction == DIR_WRITE
    assert seq == 0
    assert _last_payload(t) == bytes.fromhex(
        expected_payload_hex.replace(" ", ""))


def test_channel_volume_clamps_above_zero_db() -> None:
    """dB > 0 hard-clamps to raw=600 (0 dB) — the device's max."""
    d, t = _make_device()
    d.set_channel(0, db=+20, muted=False)  # nonsense, should clamp
    p = _last_payload(t)
    vol = int.from_bytes(p[2:4], "little")
    assert vol == 600


def test_channel_volume_clamps_below_negative_60() -> None:
    d, t = _make_device()
    d.set_channel(0, db=-200, muted=False)
    p = _last_payload(t)
    vol = int.from_bytes(p[2:4], "little")
    assert vol == 0


def test_channel_subindex_is_correct_per_cmd() -> None:
    """The subidx (payload byte 7) is fixed per channel index."""
    expected = {0: 0x01, 1: 0x02, 2: 0x03, 3: 0x07,
                4: 0x08, 5: 0x09, 6: 0x0F, 7: 0x12}
    for ch, si in expected.items():
        d, t = _make_device()
        d.set_channel(ch, db=0, muted=False)
        assert _last_payload(t)[7] == si, f"channel {ch} subidx wrong"


def test_channel_volume_then_mute_preserves_volume() -> None:
    """set_channel_volume then set_channel_mute should retain the volume
    (the cache layer makes this work despite no device readback)."""
    d, t = _make_device()
    d.set_channel_volume(0, db=-12)
    d.set_channel_mute(0, muted=True)
    # First write: en=1 vol=480 (raw)
    f1 = parse_frame(t.sent[0])
    p1 = bytes(f1.payload[:8])
    assert p1[0] == 1  # enabled
    assert int.from_bytes(p1[2:4], "little") == 480  # -12 dB
    # Second write: en=0 (muted) vol still 480
    f2 = parse_frame(t.sent[1])
    p2 = bytes(f2.payload[:8])
    assert p2[0] == 0  # muted
    assert int.from_bytes(p2[2:4], "little") == 480  # unchanged


# ── master volume + mute ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "db,muted,expected_payload_hex",
    [
        # f26449 t=230.03: master lvl=66 = +6 dB unmuted
        (+6.0, False, "42 00 00 32 00 32 01 00"),
        # f27309 t=236.90: master dragged all the way down (lvl=0 = -60 dB)
        (-60.0, False, "00 00 00 32 00 32 01 00"),
        # f27677 t=240.34: master at lvl=35 = -25 dB unmuted
        (-25.0, False, "23 00 00 32 00 32 01 00"),
        # f28793 t=249.02: master MUTED at lvl=35
        (-25.0, True, "23 00 00 32 00 32 00 00"),
        # f29417 t=253.90: master UNMUTED at lvl=35
        (-25.0, False, "23 00 00 32 00 32 01 00"),
        # f30889 t=265.34: master back to -20 dB (lvl=40 = 0x28)
        (-20.0, False, "28 00 00 32 00 32 01 00"),
    ],
)
def test_set_master_matches_capture(db, muted, expected_payload_hex) -> None:
    d, t = _make_device()
    d.set_master(db=db, muted=muted)
    cmd, cat, direction, seq = _last_meta(t)
    assert cmd == CMD_MASTER
    assert cat == CAT_STATE
    assert direction == DIR_WRITE
    assert seq == 0
    assert _last_payload(t) == bytes.fromhex(
        expected_payload_hex.replace(" ", ""))


def test_master_volume_clamps_to_db_range() -> None:
    d, t = _make_device()
    d.set_master(db=+99, muted=False)
    assert _last_payload(t)[0] == 66  # MASTER_LEVEL_MAX
    d2, t2 = _make_device()
    d2.set_master(db=-99, muted=False)
    assert _last_payload(t2)[0] == 0  # MASTER_LEVEL_MIN


def test_master_get_decodes_correctly() -> None:
    """get_master() should round-trip the payload format."""
    from dsp408.protocol import DIR_RESP, build_frame
    d, t = _make_device()
    # Queue a synthetic master read reply: lvl=50 (-10 dB), mute_bit=1 (on)
    payload = bytes([50, 0, 0, 0x32, 0, 0x32, 1, 0])
    reply_raw = build_frame(direction=DIR_RESP, seq=0, cmd=CMD_MASTER,
                            data=payload, category=CAT_STATE)
    t.queue_reply(parse_frame(reply_raw))
    db, muted = d.get_master()
    assert db == -10.0
    assert muted is False
    # Now muted
    payload2 = bytes([0, 0, 0, 0x32, 0, 0x32, 0, 0])
    reply_raw2 = build_frame(direction=DIR_RESP, seq=0, cmd=CMD_MASTER,
                             data=payload2, category=CAT_STATE)
    t.queue_reply(parse_frame(reply_raw2))
    db2, muted2 = d.get_master()
    assert db2 == -60.0
    assert muted2 is True


# ── seq=0 for writes ────────────────────────────────────────────────────
def test_writes_always_use_seq_zero() -> None:
    """Verify the seq=0 fix is enforced for every write."""
    d, t = _make_device()
    for _ in range(5):
        d.set_master(db=0, muted=False)
    for frame_bytes in t.sent:
        f = parse_frame(frame_bytes)
        assert f.seq == 0, f"write frame had seq={f.seq}, expected 0"


def test_reads_keep_auto_seq() -> None:
    """Reads must still auto-increment so we can match late replies."""
    from dsp408.protocol import CMD_GET_INFO, DIR_RESP, build_frame
    d, t = _make_device()
    # Queue 3 read replies (one per call)
    for s in (0, 1, 2):
        payload = b"hello\x00\x00\x00"
        reply = build_frame(direction=DIR_RESP, seq=s, cmd=CMD_GET_INFO,
                            data=payload, category=CAT_STATE)
        t.queue_reply(parse_frame(reply))
    for _ in range(3):
        d.read_raw(cmd=CMD_GET_INFO, category=CAT_STATE)
    # Read frames should have seq 0, 1, 2 (auto-incremented)
    seqs = [parse_frame(f).seq for f in t.sent]
    assert seqs == [0, 1, 2]


# ── channel state blob parser ───────────────────────────────────────────
def _make_channel_blob(channel: int, db: float, muted: bool,
                       delay: int = 0,
                       record_offset: int = 246) -> bytes:
    """Build a synthetic 296-byte channel-state blob with the write-format
    record embedded at `record_offset`.

    The write-format record is the same 8-byte payload the device uses:
        [en, 00, vol_lo, vol_hi, delay_lo, delay_hi, 00, subidx]
    Everything else in the blob is zeroed.
    """
    from dsp408.protocol import (
        CHANNEL_SUBIDX,
        CHANNEL_VOL_MAX,
        CHANNEL_VOL_OFFSET,
    )
    blob = bytearray(296)
    raw_vol = max(0, min(CHANNEL_VOL_MAX,
                         round(db * 10 + CHANNEL_VOL_OFFSET)))
    en_bit = 0 if muted else 1
    si = CHANNEL_SUBIDX[channel]
    record = bytes([
        en_bit, 0,
        raw_vol & 0xFF, (raw_vol >> 8) & 0xFF,
        delay & 0xFF, (delay >> 8) & 0xFF,
        0, si,
    ])
    blob[record_offset: record_offset + 8] = record
    return bytes(blob)


@pytest.mark.parametrize(
    "channel,db,muted,delay",
    [
        # f19565 t=176.04: CH1 vol back to 0 dB
        (0, 0.0, False, 0),
        # f13129 t=124.12: CH1 "volume off" — vol=0 (-60 dB), unmuted
        (0, -60.0, False, 0),
        # CH2 at -30 dB, unmuted
        (1, -30.0, False, 0),
        # CH1 muted at -60 dB
        (0, -60.0, True, 0),
        # CH1 muted, vol=0 dB (raw 600)
        (0, 0.0, True, 0),
        # CH4 (channel index 3) at -10 dB, unmuted
        (3, -10.0, False, 52),
        # CH7 (channel index 6) at -20 dB, muted
        (6, -20.0, True, 0),
        # CH8 (channel index 7) at 0 dB, unmuted
        (7, 0.0, False, 128),
    ],
)
def test_parse_channel_blob_vol_mute(channel, db, muted, delay) -> None:
    """parse_channel_state_blob extracts volume and mute correctly."""
    blob = _make_channel_blob(channel, db, muted, delay)
    result = Device.parse_channel_state_blob(blob, channel)
    assert result is not None, "parser returned None"
    assert abs(result["db"] - db) < 0.1, f"db mismatch: {result['db']} vs {db}"
    assert result["muted"] == muted
    assert result["delay"] == delay


def test_parse_channel_blob_returns_none_for_invalid_en_bit() -> None:
    """A blob with en_bit not in {0, 1} (e.g. 2) returns None."""
    blob = bytearray(296)
    blob[246] = 2   # invalid: must be 0 (muted) or 1 (audible)
    blob[248] = 88  # raw_vol=88 is valid; the en_bit check fires first
    blob[253] = 0x01  # default ch0 subidx
    result = Device.parse_channel_state_blob(bytes(blob), 0)
    assert result is None, "invalid en_bit must return None"


def test_parse_channel_blob_returns_none_for_out_of_range_vol() -> None:
    """A blob with raw_vol > CHANNEL_VOL_MAX (600) returns None."""
    from dsp408.protocol import CHANNEL_VOL_MAX
    blob = bytearray(296)
    blob[246] = 1                    # valid en_bit
    out_of_range = CHANNEL_VOL_MAX + 1  # 601
    blob[248] = out_of_range & 0xFF
    blob[249] = (out_of_range >> 8) & 0xFF
    blob[253] = 0x01
    result = Device.parse_channel_state_blob(bytes(blob), 0)
    assert result is None, "raw_vol > max must return None"


def test_parse_channel_blob_returns_none_when_too_short() -> None:
    """A blob shorter than 286 bytes returns None (can't reach end of name field)."""
    blob = bytes(285)  # one byte short of having a complete name field
    result = Device.parse_channel_state_blob(blob, 0)
    assert result is None


def test_parse_channel_blob_all_channels_read_from_fixed_offset() -> None:
    """Parser reads the per-channel record from the fixed offset 246..253.

    Each channel's blob is built with the correct record at the canonical
    offset (246).  The parser should find it, ignore bytes elsewhere, and
    return the right volume for each channel.
    """
    from dsp408.protocol import CHANNEL_SUBIDX

    for ch in range(8):
        raw_vol = (ch + 1) * 50   # 50, 100, ..., 400 — all valid
        expected_db = (raw_vol - 600) / 10.0
        blob = _make_channel_blob(ch, expected_db, muted=False)
        # Sanity-check that our helper puts the record at offset 246.
        assert blob[253] == CHANNEL_SUBIDX[ch], (
            f"helper placed wrong subidx for ch {ch}"
        )
        result = Device.parse_channel_state_blob(blob, ch)
        assert result is not None, f"channel {ch}: parser returned None"
        assert abs(result["db"] - expected_db) < 0.1, (
            f"ch {ch}: db={result['db']} expected {expected_db}"
        )


def test_parse_channel_blob_returns_actual_subidx() -> None:
    """The parser returns the actual blob[253] value, not the table default.

    Devices can have non-default DSP types (e.g. device 1's ch1 uses
    subidx=0x12).  The 'subidx' key in the result must reflect what the
    firmware stored, so callers can preserve it on write-back.
    """
    blob = bytearray(296)
    blob[246] = 1           # en_bit: audible
    blob[248] = 0x58        # raw_vol = 0x0258 = 600 → 0 dB
    blob[249] = 0x02
    blob[253] = 0x12        # non-default type (e.g. device 1 ch1 on live hw)
    result = Device.parse_channel_state_blob(bytes(blob), 0)
    assert result is not None, "valid blob must parse successfully"
    assert result["db"] == 0.0
    assert result["muted"] is False
    assert result["subidx"] == 0x12, (
        "returned subidx must match blob[253], not the CHANNEL_SUBIDX table"
    )


def test_parse_channel_blob_all_zeros_parses_as_muted_silent() -> None:
    """An all-zeros 296-byte blob (uninitialized channel) parses as
    muted=True, db=-60 dB — not as an error.  The subidx=0x00 is returned
    as-is so the caller can decide how to handle uninitialized channels.
    """
    blob = bytes(296)  # all zeros: en_bit=0, raw_vol=0, subidx=0
    result = Device.parse_channel_state_blob(blob, 0)
    assert result is not None, "all-zeros blob should parse (en_bit=0 is valid)"
    assert result["muted"] is True   # en_bit=0 → muted
    assert result["db"] == -60.0     # raw_vol=0 → -60 dB
    assert result["subidx"] == 0x00  # returns whatever is at blob[253]


def test_parse_channel_blob_decodes_extended_fields() -> None:
    """Verify the full 296-byte blob layout: phase, crossover, mixer,
    compressor, link group, name — fields newly decoded from the leon
    v1.23 Android app + verified live on Pi hardware.
    """
    from dsp408.protocol import (
        CHANNEL_SUBIDX,
        FILTER_TYPE_BESSEL,
        FILTER_TYPE_LR,
        OFF_ALL_PASS_Q,
        OFF_ATTACK_MS,
        OFF_DELAY,
        OFF_EQ_MODE,
        OFF_GAIN,
        OFF_HPF_FILTER,
        OFF_HPF_FREQ,
        OFF_HPF_SLOPE,
        OFF_LINKGROUP,
        OFF_LPF_FILTER,
        OFF_LPF_FREQ,
        OFF_LPF_SLOPE,
        OFF_MIXER,
        OFF_MUTE,
        OFF_NAME,
        OFF_POLAR,
        OFF_RELEASE_MS,
        OFF_SPK_TYPE,
        OFF_THRESHOLD,
    )
    blob = bytearray(296)
    blob[OFF_MUTE] = 1                        # audible
    blob[OFF_POLAR] = 1                       # phase inverted
    blob[OFF_GAIN:OFF_GAIN + 2] = (480).to_bytes(2, "little")    # -12 dB
    blob[OFF_DELAY:OFF_DELAY + 2] = (52).to_bytes(2, "little")   # 52 samples
    blob[OFF_EQ_MODE] = 1                     # EQ on
    blob[OFF_SPK_TYPE] = CHANNEL_SUBIDX[3]    # 0x07 = fr_low
    blob[OFF_HPF_FREQ:OFF_HPF_FREQ + 2] = (80).to_bytes(2, "little")
    blob[OFF_HPF_FILTER] = FILTER_TYPE_LR     # Linkwitz-Riley
    blob[OFF_HPF_SLOPE] = 3                   # 24 dB/oct
    blob[OFF_LPF_FREQ:OFF_LPF_FREQ + 2] = (5000).to_bytes(2, "little")
    blob[OFF_LPF_FILTER] = FILTER_TYPE_BESSEL
    blob[OFF_LPF_SLOPE] = 1                   # 12 dB/oct
    blob[OFF_MIXER:OFF_MIXER + 8] = bytes([100, 50, 0, 0, 0, 0, 0, 0])
    blob[OFF_ALL_PASS_Q:OFF_ALL_PASS_Q + 2] = (420).to_bytes(2, "little")
    blob[OFF_ATTACK_MS:OFF_ATTACK_MS + 2] = (10).to_bytes(2, "little")
    blob[OFF_RELEASE_MS:OFF_RELEASE_MS + 2] = (250).to_bytes(2, "little")
    blob[OFF_THRESHOLD] = 12                  # threshold raw
    blob[OFF_LINKGROUP] = 2                   # group 2
    blob[OFF_NAME:OFF_NAME + 8] = b"TWEETER\x00"

    result = Device.parse_channel_state_blob(bytes(blob), 3)
    assert result is not None
    # legacy fields still present
    assert result["db"] == -12.0
    assert result["muted"] is False
    assert result["delay"] == 52
    assert result["subidx"] == 0x07
    # new fields
    assert result["polar"] is True
    assert result["eq_mode"] == 1
    assert result["spk_type"] == 0x07
    assert result["hpf"] == {"freq": 80, "filter": FILTER_TYPE_LR, "slope": 3}
    assert result["lpf"] == {
        "freq": 5000, "filter": FILTER_TYPE_BESSEL, "slope": 1,
    }
    assert result["mixer"] == [100, 50, 0, 0, 0, 0, 0, 0]
    assert result["compressor"] == {
        "all_pass_q": 420,
        "attack_ms": 10,
        "release_ms": 250,
        "threshold": 12,
    }
    assert result["linkgroup"] == 2
    assert result["name"] == "TWEETER"
    # raw blob preserved
    assert isinstance(result["raw"], bytes)
    assert len(result["raw"]) == 296


def test_get_channel_updates_cache_with_discovered_subidx() -> None:
    """get_channel() should store the actual blob[253] subidx in the cache,
    so subsequent set_channel() calls preserve the firmware's DSP type.

    The 296-byte channel read response is multi-frame on real hardware.  We
    bypass HID framing by injecting a pre-assembled Frame directly into the
    FakeTransport queue (FakeTransport.read_response() returns queued Frames
    as-is, skipping HID reassembly).
    """
    from dsp408.protocol import CMD_READ_CHANNEL_BASE, DIR_RESP

    d, t = _make_device()
    # Build a synthetic 296-byte blob for ch1 with non-default subidx=0x12
    non_default_si = 0x12
    blob = bytearray(296)
    blob[246] = 1       # audible
    blob[248] = 0x58    # raw_vol low byte  \ 0x0258 = 600 → 0 dB
    blob[249] = 0x02    # raw_vol high byte /
    blob[253] = non_default_si
    cmd = (CMD_READ_CHANNEL_BASE << 8) | 1  # 0x7701

    # Inject as a pre-assembled Frame — FakeTransport returns queued frames
    # directly, so the 296-byte payload arrives without HID framing limits.
    synth_frame = Frame(
        direction=DIR_RESP,
        seq=0,
        category=CAT_PARAM,
        cmd=cmd,
        payload_len=len(blob),
        payload=bytes(blob),
        checksum=0,
        checksum_ok=True,
        raw=b"\x00" * 64,  # placeholder; Device._exchange only checks .cmd
    )
    t.queue_reply(synth_frame)

    state = d.get_channel(1)
    assert state["subidx"] == non_default_si, (
        "get_channel must return the actual blob[253] as subidx"
    )

    # Now set_channel on ch1 — it should use the cached subidx=0x12, not 0x02
    d.set_channel(1, db=0.0, muted=False)
    payload = _last_payload(t)
    assert payload[7] == non_default_si, (
        f"set_channel must preserve discovered subidx=0x12, got {payload[7]:#04x}"
    )
