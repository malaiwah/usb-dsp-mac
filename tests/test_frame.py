"""Verify frame build/parse against exact bytes captured on the wire.

Each test case is a real HID frame copied from
`captures/windows-01-fw-update-original-V6.21.pcapng`, so a round-trip
here proves our framing exactly matches the official DSP-408.exe V1.24.
"""
from __future__ import annotations

import pytest

from dsp408.protocol import (
    CAT_PARAM,
    CAT_STATE,
    CMD_CONNECT,
    CMD_GET_INFO,
    CMD_PRESET_NAME,
    DIR_CMD,
    DIR_WRITE,
    CMD_STATE_0x13,
    build_frame,
    parse_frame,
    xor_checksum,
)


def _h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# Each tuple: (human label, hex frame copied from capture, expected (cmd, dir, cat, seq, len))
WIRE_FRAMES = [
    (
        "cmd=0xcc CONNECT request (frame #11)",
        "808080eea2010009cc000000080000000000000000006eaa00000000000000000000000000000000000000000000000000000000000000000000000000000000",
        (CMD_CONNECT, DIR_CMD, CAT_STATE, 0, 8),
    ),
    (
        "cmd=0x04 GET_INFO request (frame #15)",
        "808080eea20100090400000008000000000000000000a6aa00000000000000000000000000000000000000000000000000000000000000000000000000000000",
        (CMD_GET_INFO, DIR_CMD, CAT_STATE, 0, 8),
    ),
    (
        "cmd=0x00 PRESET_NAME request seq=1 (frame #19)",
        "808080eea20101090000000008000000000000000000a3aa00000000000000000000000000000000000000000000000000000000000000000000000000000000",
        (CMD_PRESET_NAME, DIR_CMD, CAT_STATE, 1, 8),
    ),
    (
        "cmd=0x13 STATE_0x13 request (frame #43)",
        "808080eea20100091300000008000000000000000000b1aa00000000000000000000000000000000000000000000000000000000000000000000000000000000",
        (CMD_STATE_0x13, DIR_CMD, CAT_STATE, 0, 8),
    ),
    (
        "cmd=0x7700 read-channel request (frame #67, cat=0x04)",
        "808080eea20100040077000008000000000000000000d8aa00000000000000000000000000000000000000000000000000000000000000000000000000000000",
        (0x7700, DIR_CMD, CAT_PARAM, 0, 8),
    ),
]



@pytest.mark.parametrize("label,hex_frame,expected", WIRE_FRAMES)
def test_parse_wire_frame(label, hex_frame, expected):
    raw = _h(hex_frame)
    assert len(raw) == 64, f"{label}: expected 64-byte frame, got {len(raw)}"
    f = parse_frame(raw)
    assert f is not None, label
    exp_cmd, exp_dir, exp_cat, exp_seq, exp_len = expected
    assert f.cmd == exp_cmd, f"{label}: cmd mismatch"
    assert f.direction == exp_dir, f"{label}: direction mismatch"
    assert f.category == exp_cat, f"{label}: category mismatch"
    assert f.seq == exp_seq, f"{label}: seq mismatch"
    assert f.payload_len == exp_len, f"{label}: len mismatch"
    assert f.checksum_ok, f"{label}: checksum failed"


@pytest.mark.parametrize("label,hex_frame,expected", WIRE_FRAMES)
def test_build_matches_wire(label, hex_frame, expected):
    """Rebuilding the frame from fields must produce identical bytes."""
    raw = _h(hex_frame)
    cmd, direction, cat, seq, _ = expected
    built = build_frame(
        direction=direction,
        seq=seq,
        cmd=cmd,
        data=b"\x00" * 8,
        category=cat,
    )
    assert built == raw, f"{label}: round-trip mismatch\nbuilt:    {built.hex()}\nexpected: {raw.hex()}"


def test_checksum_invariant():
    """XOR is self-inverse: appending chk to data makes overall XOR zero."""
    data = bytes(range(16))
    chk = xor_checksum(data)
    assert xor_checksum(data + bytes([chk])) == 0


def test_write_frame_roundtrip_1f07():
    """Re-build a 0x1f07 volume write and confirm it matches frame 731
    from captures/windows-04c-stream-nostream-stream.pcapng."""
    wire = _h(
        "808080eea1010004071f00000800010096010000001230aa"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    )[:64]
    # Payload dissection: 01 00 | value_le_u32 = 0x00000196 | 00 | sub=0x12
    payload = b"\x01\x00" + (0x0196).to_bytes(4, "little") + b"\x00" + b"\x12"
    built = build_frame(
        direction=DIR_WRITE,
        seq=0,
        cmd=0x1F07,
        data=payload,
        category=CAT_PARAM,
    )
    assert built == wire
    f = parse_frame(built)
    assert f is not None
    assert f.cmd == 0x1F07
    assert f.direction == DIR_WRITE
    assert f.category == CAT_PARAM
    assert f.payload == payload
    assert f.checksum_ok


def test_parse_rejects_non_dsp408():
    assert parse_frame(b"\x00" * 64) is None
    # wrong magic
    bad = b"\x80\x80\x80\xff" + b"\x00" * 60
    assert parse_frame(bad) is None


def test_write_preset_name_roundtrip():
    """Frame #191 from captures/windows-04c-stream-nostream-stream.pcapng:
    rename the active preset to "Custom". Validates the preset-name
    write payload layout (16-byte payload, length 0x10)."""
    wire = _h(
        "808080eea1010009000000001000437573746f6d000000000000000000008aaa"
        "00000000000000000000000000000000000000000000000000000000000000000000"
    )[:64]
    # Payload: "Custom" + 10 null bytes = 16 bytes
    payload = b"Custom".ljust(16, b"\x00")
    built = build_frame(
        direction=DIR_WRITE,
        seq=0,
        cmd=CMD_PRESET_NAME,
        data=payload,
        category=CAT_STATE,
    )
    assert built == wire
    f = parse_frame(built)
    assert f is not None
    assert f.cmd == CMD_PRESET_NAME
    assert f.direction == DIR_WRITE
    assert f.category == CAT_STATE
    assert f.payload_len == 16
    assert f.payload.rstrip(b"\x00") == b"Custom"
    assert f.checksum_ok


def test_is_multi_frame_flag():
    """Declared payload > 48 bytes should set is_multi_frame()."""
    wire = _h(
        "808080ee530100040077000028011f0058023400000041005802340000007d00580234000000fa00580234000000f401580234000000e803580234000000d007"
    )
    f = parse_frame(wire[:64])
    assert f is not None
    assert f.payload_len == 0x128  # 296
    assert f.is_multi_frame()
