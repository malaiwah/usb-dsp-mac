"""dsp408.protocol — low-level HID frame building and parsing.

Wire format (64-byte HID report on EP 0x01 OUT / EP 0x82 IN):

    offset  len  field           notes
    0       4    magic           80 80 80 ee
    4       1    direction       host→dev: a2 (read request) / a1 (write/fw)
                                 dev→host: 53 (read reply)  / 51 (write ack)
    5       1    version         always 01
    6       1    seq             8-bit sequence number (host chooses for OUT)
    7       1    category        0x09 = state/global cmd, 0x04 = param cmd
    8..11   4    cmd             LE u32 command code
    12..13  2    payload length  LE u16
    14..N   len  payload         up to ~48 bytes in a single frame
    14+len  1    checksum        XOR of bytes[4 .. 14+len-1]
    15+len  1    end marker      aa
    rest         padding         00 00 ...

Direction pairs:
    a2 (host→dev READ)  ↔ 53 (dev→host READ reply)
    a1 (host→dev WRITE) ↔ 51 (dev→host WRITE ack)

The device mirrors cmd, seq, and category in its reply, so a host can
match replies to requests by (seq, cmd, category).

Multi-frame reads:
    Some reads return payloads larger than one frame (e.g. cmd=0x77NN
    returns 296 bytes across ~5 IN frames). Only the first frame carries
    the 16-byte header and declared length; subsequent frames are raw 64-
    byte continuations whose bytes are concatenated until the declared
    length is satisfied. Use `TransportReader` in dsp408.transport for
    this.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# USB IDs
VID = 0x0483
PID = 0x5750

# Fixed header bytes
FRAME_MAGIC = bytes([0x80, 0x80, 0x80, 0xEE])
END_MARKER = 0xAA
PROTO_VERSION = 0x01
FRAME_SIZE = 64
HEADER_SIZE = 14  # bytes before payload

# Direction byte
DIR_CMD = 0xA2        # host → device, READ request
DIR_WRITE = 0xA1      # host → device, WRITE / firmware
DIR_RESP = 0x53       # device → host, READ reply
DIR_WRITE_ACK = 0x51  # device → host, WRITE / firmware ack

# Category byte (byte[7])
CAT_STATE = 0x09      # connect, get_info, preset name, status, idle poll, firmware blocks
CAT_PARAM = 0x04      # parameter read/write (0x77NN, 0x1fNN, 0x2000)


def category_hint(cmd: int) -> int:
    """Pick the right `category` byte for a given command code.

    Parameter commands (0x77NN, 0x1fNN, 0x2000) use category 0x04
    (CAT_PARAM); everything else uses 0x09 (CAT_STATE). Mirrors what
    DSP-408.exe V1.24 emits in the Windows captures and used by the CLI
    / Gradio UI / MQTT bridge to default the category correctly.
    """
    if 0x7700 <= cmd <= 0x77FF:
        return CAT_PARAM
    if 0x1F00 <= cmd <= 0x1FFF:
        return CAT_PARAM
    if cmd == 0x2000:
        return CAT_PARAM
    return CAT_STATE


def xor_checksum(data: bytes) -> int:
    """XOR of all bytes — matches device checksum over frame[4 .. len-1]."""
    c = 0
    for b in data:
        c ^= b
    return c & 0xFF


def build_frame(
    direction: int,
    seq: int,
    cmd: int,
    data: bytes = b"\x00" * 8,
    category: int = CAT_STATE,
) -> bytes:
    """Build a 64-byte DSP-408 HID frame.

    Args:
        direction: one of DIR_CMD / DIR_WRITE (for host→device frames).
        seq: 8-bit sequence number.
        cmd: command code (any 32-bit LE unsigned int; observed values
            fit in u32 with upper 16 bits typically zero, except the
            register-style cmds 0x77NN and 0x1fNN).
        data: payload bytes (default: 8 zero bytes, which is what the
            official app sends for every read request).
        category: byte[7] — 0x09 for "state" commands (connect, info,
            preset name, firmware blocks); 0x04 for parameter-level
            reads/writes (0x77NN, 0x1fNN, 0x2000).

    Returns:
        Exactly 64 bytes.
    """
    payload_len = len(data)
    header = struct.pack(
        "<4sBBBB I H",  # magic(4) | dir | ver | seq | cat | cmd_le32 | len_le16
        FRAME_MAGIC,
        direction & 0xFF,
        PROTO_VERSION,
        seq & 0xFF,
        category & 0xFF,
        cmd & 0xFFFFFFFF,
        payload_len & 0xFFFF,
    )
    body = header + data
    chk = xor_checksum(body[4:])          # XOR from direction onwards
    tail = bytes([chk, END_MARKER])
    frame = body + tail
    if len(frame) > FRAME_SIZE:
        raise ValueError(f"frame too long: {len(frame)} > {FRAME_SIZE}")
    return frame + b"\x00" * (FRAME_SIZE - len(frame))


@dataclass
class Frame:
    """Decoded DSP-408 frame."""

    direction: int
    seq: int
    category: int
    cmd: int
    payload_len: int  # declared length (may span multiple HID frames)
    payload: bytes  # only the bytes present in THIS 64-byte frame
    checksum: int
    checksum_ok: bool
    raw: bytes  # full 64 bytes received

    def is_reply(self) -> bool:
        return self.direction in (DIR_RESP, DIR_WRITE_ACK)

    def is_multi_frame(self) -> bool:
        """True if the declared payload is larger than what fits in one HID frame."""
        # Max payload in one frame = FRAME_SIZE - HEADER - 2 (chk + end) = 48 bytes
        return self.payload_len > FRAME_SIZE - HEADER_SIZE - 2


def parse_frame(raw: bytes) -> Frame | None:
    """Parse a raw HID frame. Returns None if not DSP-408 format.

    Checksum verification is lenient: returns a Frame with
    `checksum_ok=False` rather than raising — useful for the multi-frame
    case where the continuation frames don't have their own checksum.
    """
    if len(raw) < HEADER_SIZE + 2:
        return None
    if raw[:4] != FRAME_MAGIC:
        return None
    direction = raw[4]
    version = raw[5]
    if version != PROTO_VERSION:
        return None
    seq = raw[6]
    category = raw[7]
    cmd = struct.unpack_from("<I", raw, 8)[0]
    payload_len = struct.unpack_from("<H", raw, 12)[0]

    # Bytes actually present in this frame (may be less than payload_len)
    max_in_frame = FRAME_SIZE - HEADER_SIZE - 2
    present = min(payload_len, max_in_frame, len(raw) - HEADER_SIZE - 2)
    if present < 0:
        present = 0
    payload = bytes(raw[HEADER_SIZE : HEADER_SIZE + present])

    chk_pos = HEADER_SIZE + present
    if chk_pos < len(raw):
        checksum = raw[chk_pos]
        chk_region = raw[4:chk_pos]
        checksum_ok = xor_checksum(chk_region) == checksum
    else:
        checksum = 0
        checksum_ok = False

    return Frame(
        direction=direction,
        seq=seq,
        category=category,
        cmd=cmd,
        payload_len=payload_len,
        payload=payload,
        checksum=checksum,
        checksum_ok=checksum_ok,
        raw=bytes(raw),
    )


# ── Known command codes ────────────────────────────────────────────────
# All values observed on the wire in captures/windows-*.pcapng

# "State" commands (byte[7] = 0x09)
CMD_CONNECT = 0xCC              # first packet; ack=1 byte 0x00
CMD_IDLE_POLL = 0x03            # app spams this every ~30 ms; returns preset name
CMD_GET_INFO = 0x04             # returns "MYDW-AV1.06" (device identity)
CMD_PRESET_NAME = 0x00          # read: returns 15-byte name; write(a1): set name
CMD_STATE_0x13 = 0x13           # returns 10 bytes, meaning unknown (levels?)
CMD_STATUS = 0x34               # returns 1 byte (0x00 = idle)
CMD_GLOBAL_0x02 = 0x02          # returns 8 bytes: 01 00 01 00 00 00 00 00
CMD_GLOBAL_0x05 = 0x05          # returns 8 bytes: 28 00 00 32 00 32 01 00
CMD_GLOBAL_0x06 = 0x06          # returns 8 bytes: 03 09 04 0a 0f 12 16 17 (8-ch)

# Firmware commands (byte[7] = 0x09, direction = a1 WRITE)
CMD_FW_PREP = 0x36              # 16 zero bytes; triggers USB re-enumeration
CMD_FW_META = 0x37              # 4 bytes from .bin WMCU header offset 4
CMD_FW_BLOCK = 0x38             # 48 bytes of firmware per packet
CMD_FW_APPLY = 0x39             # 1 byte 0x13; applies and reboots

# Parameter commands (byte[7] = 0x04)
# Channel-level reads: 0x7700 + channel_index (0..7), returns 296 bytes
CMD_READ_CHANNEL_BASE = 0x0077
# Channel-level writes (0x1f00..0x1f07, one cmd per output channel).
# Payload is 8 bytes:
#     [0]  enable    (1 = audible, 0 = muted)
#     [1]  reserved  (always 0 in captures)
#     [2..3]  vol_le_u16     dB = (raw - 600) / 10  (0..600 = -60..0 dB)
#     [4..5]  delay_le_u16   samples (8 ms = 384 @ 48 kHz)
#     [6]  reserved  (always 0)
#     [7]  subidx    one of {0x01, 0x02, 0x03, 0x07, 0x08, 0x09, 0x0f, 0x12}
#                    for cmd index 0..7 respectively. The device echoes the
#                    subidx back; not currently understood as a separate
#                    parameter selector.
# Verified live on hardware: 0x1f00→Out1 (Left), 0x1f01→Out2 (Right),
# 0x1f02→Out3, 0x1f03→Out4 (sequential mapping).
CMD_WRITE_CHANNEL_BASE = 0x1F00

# Output-routing matrix writes (0x2100..0x2107 = Out1..Out8).
# Payload is 8 bytes; bytes [0..3] = IN1..IN4 routing levels
# (0x64 = ON / +0 dB unity, 0x00 = OFF). Bytes [4..7] always 0.
CMD_ROUTING_BASE = 0x2100

# Master volume + mute. Lives on the cat=0x09 plane (CMD_GLOBAL_0x05).
# Payload is 8 bytes:
#     [0]  level    dB = level - 60      (0..66 = -60..+6 dB)
#     [1..5]  constants 00 00 32 00 32   (never observed to change)
#     [6]  mute_bit (1 = unmuted/audible, 0 = muted) — note the inverted
#                    polarity vs per-channel byte[0]
#     [7]  reserved (always 0)
# Same cmd code as CMD_GLOBAL_0x05; alias for self-documenting call sites.
CMD_MASTER = 0x0005

CMD_WRITE_GLOBAL = 0x2000       # writes global params (layout TBD)

# Master payload constants
MASTER_LEVEL_MIN = 0     # raw = -60 dB
MASTER_LEVEL_MAX = 66    # raw = +6 dB
MASTER_LEVEL_OFFSET = 60  # raw = dB + 60

# Per-channel volume constants
CHANNEL_VOL_MIN = 0      # raw = -60 dB
CHANNEL_VOL_MAX = 600    # raw = 0 dB (unity)
CHANNEL_VOL_OFFSET = 600  # raw = (dB * 10) + 600

# Subidx for each of the 8 channel write cmds. Order matches cmd index 0..7.
# These are also the *spk_type* (speaker-role) values stored at blob[253] of
# the per-channel state — confirmed by cross-reference with the official
# Android app's leon.android.chs_ydw_dcs480_dsp_408 v1.23 (DataStruct_Output
# field naming + the 25-entry speaker-role table).
CHANNEL_SUBIDX = (0x01, 0x02, 0x03, 0x07, 0x08, 0x09, 0x0F, 0x12)

# Routing input-on / off levels
ROUTING_ON = 0x64
ROUTING_OFF = 0x00

# ── Per-channel state blob (cmd=0x77NN response) field offsets ─────────────
# Decoded from Android leon v1.23 DataOptUtil.java:1351–1465, then verified
# live on real DSP-408 hardware (see notes/blob-layout-verification.md on
# the reverse-engineering branch). The full blob is 296 bytes; offsets 0..245
# carry the parametric-EQ region (see below).
#
# IMPORTANT: leon's app expects 31 EQ bands (248 bytes). Our firmware variant
# has the basic record at offset 246 instead of 248 — a 2-byte shift implying
# 30 EQ bands (240 bytes) plus 6 bytes of header/padding before the basic
# record. EQ count + stride still need confirmation via a Windows-USB
# capture of single-band tweaks.
BLOB_SIZE = 296

# Basic per-channel record (8 bytes at 246..253) — also the write payload format
# accepted by cmd=0x1FNN.
OFF_MUTE        = 246  # 1=audible, 0=muted (INVERTED from leon's polarity)
OFF_POLAR       = 247  # phase invert: 0=normal, 1=inverted (180°)
OFF_GAIN        = 248  # u16 LE; raw = (dB * 10) + 600; range 0..600 = -60..0 dB
OFF_DELAY       = 250  # u16 LE; samples (or cm-step index)
OFF_EQ_MODE     = 252  # EQ enable/bypass flag
OFF_SPK_TYPE    = 253  # speaker-role index; one of CHANNEL_SUBIDX by default

# Crossover (HPF + LPF) — 8 bytes at 254..261
OFF_HPF_FREQ    = 254  # u16 LE Hz (or table index)
OFF_HPF_FILTER  = 256  # 0=Butterworth, 1=Bessel, 2=Linkwitz-Riley
OFF_HPF_SLOPE   = 257  # 0..7 = 6/12/18/24/30/36/42/48 dB/oct, 8=Off
OFF_LPF_FREQ    = 258
OFF_LPF_FILTER  = 260
OFF_LPF_SLOPE   = 261

# Mixer — 8 bytes at 262..269 (one u8 percentage per input source IN1..IN8).
# leon's data model has 16 cells per output but our firmware exposes 8.
OFF_MIXER       = 262
MIXER_CELLS     = 8

# Compressor / dynamics + link group — 8 bytes at 270..277
OFF_ALL_PASS_Q  = 270  # u16 LE
OFF_ATTACK_MS   = 272  # u16 LE
OFF_RELEASE_MS  = 274  # u16 LE
OFF_THRESHOLD   = 276  # u8 (encoding TBD; likely dB-scaled)
OFF_LINKGROUP   = 277  # u8 channel link/group index (0 = no link)

# Per-channel name — 8 bytes ASCII at 278..285
OFF_NAME        = 278
NAME_LEN        = 8

# Filter type / slope enums (for select-style controls in MQTT discovery).
FILTER_TYPE_BW = 0
FILTER_TYPE_BESSEL = 1
FILTER_TYPE_LR = 2
FILTER_TYPE_NAMES = ("Butterworth", "Bessel", "Linkwitz-Riley")

# Slope enum: index → "6 dB/oct" / "12 dB/oct" / ... / "Off"
SLOPE_NAMES = (
    "6 dB/oct",   # 0
    "12 dB/oct",  # 1
    "18 dB/oct",  # 2
    "24 dB/oct",  # 3 — manual stops here, but firmware accepts more
    "30 dB/oct",  # 4
    "36 dB/oct",  # 5
    "42 dB/oct",  # 6
    "48 dB/oct",  # 7
    "Off",        # 8
)

# Speaker-role names (25-entry table from the leon app's arrays.xml).
# Index = blob[OFF_SPK_TYPE] value. The factory CHANNEL_SUBIDX assignments
# pick a sparse subset of these.
SPK_TYPE_NAMES = (
    "none",       # 0
    "fl_high",    # 1   = CHANNEL_SUBIDX[0]
    "fl_mid",     # 2   = CHANNEL_SUBIDX[1]
    "fl_low",     # 3   = CHANNEL_SUBIDX[2]
    "fl",         # 4
    "fr_high",    # 5
    "fr_mid",     # 6
    "fr_low",     # 7   = CHANNEL_SUBIDX[3]
    "fr",         # 8   = CHANNEL_SUBIDX[4]
    "rl_high",    # 9   = CHANNEL_SUBIDX[5] (best guess from pattern)
    "rl_mid",     # 10
    "rl_low",     # 11
    "rl",         # 12
    "rr_high",    # 13
    "rr_mid",     # 14
    "rr_low",     # 15  = CHANNEL_SUBIDX[6]
    "rr",         # 16
    "center",     # 17
    "sub",        # 18  = CHANNEL_SUBIDX[7]
    "sub_l",      # 19
    "sub_r",      # 20
    "aux1",       # 21
    "aux2",       # 22
    "aux3",       # 23
    "aux4",       # 24
)


__all__ = [
    "VID",
    "PID",
    "FRAME_MAGIC",
    "END_MARKER",
    "PROTO_VERSION",
    "FRAME_SIZE",
    "HEADER_SIZE",
    "DIR_CMD",
    "DIR_WRITE",
    "DIR_RESP",
    "DIR_WRITE_ACK",
    "CAT_STATE",
    "CAT_PARAM",
    "category_hint",
    "xor_checksum",
    "build_frame",
    "parse_frame",
    "Frame",
    # command codes
    "CMD_CONNECT",
    "CMD_IDLE_POLL",
    "CMD_GET_INFO",
    "CMD_PRESET_NAME",
    "CMD_STATE_0x13",
    "CMD_STATUS",
    "CMD_GLOBAL_0x02",
    "CMD_GLOBAL_0x05",
    "CMD_GLOBAL_0x06",
    "CMD_FW_PREP",
    "CMD_FW_META",
    "CMD_FW_BLOCK",
    "CMD_FW_APPLY",
    "CMD_READ_CHANNEL_BASE",
    "CMD_WRITE_CHANNEL_BASE",
    "CMD_WRITE_GLOBAL",
    "CMD_ROUTING_BASE",
    "CMD_MASTER",
    "MASTER_LEVEL_MIN",
    "MASTER_LEVEL_MAX",
    "MASTER_LEVEL_OFFSET",
    "CHANNEL_VOL_MIN",
    "CHANNEL_VOL_MAX",
    "CHANNEL_VOL_OFFSET",
    "CHANNEL_SUBIDX",
    "ROUTING_ON",
    "ROUTING_OFF",
    # blob layout
    "BLOB_SIZE",
    "OFF_MUTE", "OFF_POLAR", "OFF_GAIN", "OFF_DELAY",
    "OFF_EQ_MODE", "OFF_SPK_TYPE",
    "OFF_HPF_FREQ", "OFF_HPF_FILTER", "OFF_HPF_SLOPE",
    "OFF_LPF_FREQ", "OFF_LPF_FILTER", "OFF_LPF_SLOPE",
    "OFF_MIXER", "MIXER_CELLS",
    "OFF_ALL_PASS_Q", "OFF_ATTACK_MS", "OFF_RELEASE_MS",
    "OFF_THRESHOLD", "OFF_LINKGROUP",
    "OFF_NAME", "NAME_LEN",
    # enums
    "FILTER_TYPE_BW", "FILTER_TYPE_BESSEL", "FILTER_TYPE_LR",
    "FILTER_TYPE_NAMES", "SLOPE_NAMES", "SPK_TYPE_NAMES",
]
