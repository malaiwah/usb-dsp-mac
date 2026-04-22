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

# Category byte (byte[7]) — leon's code calls this "DataType":
#   3 = MUSIC  (input-side processing: input EQ, MISC, noisegate)
#   4 = OUTPUT (per-output channel processing: output EQ, crossover, etc.)
#   9 = SYSTEM (preset name, master, identity, firmware, magic registers)
CAT_INPUT = 0x03      # input processing — verified live 2026-04-19
CAT_STATE = 0x09      # connect, get_info, preset name, status, idle poll, firmware blocks
CAT_PARAM = 0x04      # output parameter read/write (0x77NN, 0x1fNN, 0x2000, etc.)


def category_hint(cmd: int) -> int:
    """Pick the right `category` byte for a given command code.

    Parameter commands (0x77NN, 0x1fNN, 0x2000, 0x21NN, 0x100BC, 0x120NN)
    use category 0x04 (CAT_PARAM); everything else uses 0x09 (CAT_STATE).
    Mirrors what DSP-408.exe V1.24 emits in the Windows captures and
    used by the CLI / Gradio UI / MQTT bridge to default the category
    correctly.
    """
    if 0x7700 <= cmd <= 0x77FF:
        return CAT_PARAM
    if 0x1F00 <= cmd <= 0x1FFF:
        return CAT_PARAM
    if cmd == 0x2000:
        return CAT_PARAM
    if 0x2100 <= cmd <= 0x24FF:  # routing/compressor/name + future 0x22NN/0x23NN/0x24NN
        return CAT_PARAM
    # EQ band writes 0x10000..0x10FFF and crossover 0x12000..0x12007
    if 0x10000 <= cmd <= 0x12FFF:
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
        raise ValueError(
            f"frame too long: {len(frame)} > {FRAME_SIZE} — payload "
            f"is {len(data)} bytes; use build_frames_multi() for "
            f"payloads > {FRAME_SIZE - HEADER_SIZE - 2} bytes"
        )
    return frame + b"\x00" * (FRAME_SIZE - len(frame))


def build_frames_multi(
    *,
    direction: int = DIR_CMD,
    seq: int = 0,
    cmd: int = 0,
    data: bytes = b"\x00" * 8,
    category: int = CAT_STATE,
) -> list[bytes]:
    """Build a list of 64-byte HID frames carrying ONE logical DSP-408 frame.

    Single-frame and multi-frame layouts are subtly different — both are
    extracted from the Windows GUI captures:

    Single-frame (payload ≤ 48 bytes):
        ``magic(4) | dir | ver | seq | cat | cmd_le32 | len_le16 |
         payload | xor_chk | END_MARKER | zero-pad``
        (= 14-byte header + N payload + 2-byte trailer, padded to 64)

    Multi-frame (payload > 48 bytes — e.g. 296-byte channel-state writes
    the GUI emits for "Load from disk"):
        First frame: ``magic | header[10] | first_50_bytes_of_payload``
            (= 14 header + 50 payload, NO checksum, NO end marker)
        Continuation frames: raw 64-byte chunks of remaining payload.
        Last continuation frame: payload bytes are followed *in the
            same HID report* by ``xor_chk | END_MARKER``, then
            zero-pad to fill the 64-byte HID report.  The XOR
            checksum covers ``[direction..end_of_payload]`` (header
            without the 4-byte magic prefix, plus all payload bytes).
        ``payload_len`` in the header carries the FULL declared length;
        the firmware reads it and consumes continuation HID reports
        until the declared count is satisfied, then expects the
        checksum + end marker immediately after.

    Wire pattern verified against ``captures/load_loaddisk_save_preset_bureau.pcapng``
    frames 2019..2027 (a 296-byte cmd=0x10000 write). Byte-exact
    replay of those 5 frames was acked by the device 2026-04-19.
    """
    max_in_first_single = FRAME_SIZE - HEADER_SIZE - 2  # 48: room for chk+end
    if len(data) <= max_in_first_single:
        return [build_frame(direction=direction, seq=seq, cmd=cmd,
                            data=data, category=category)]
    # Multi-frame: first frame has NO chk/end, 50 payload bytes fit.
    max_in_first_multi = FRAME_SIZE - HEADER_SIZE  # 50
    full_len = len(data)
    first_chunk = data[:max_in_first_multi]
    header = struct.pack(
        "<4sBBBB I H",
        FRAME_MAGIC,
        direction & 0xFF,
        PROTO_VERSION,
        seq & 0xFF,
        category & 0xFF,
        cmd & 0xFFFFFFFF,
        full_len & 0xFFFF,  # FULL declared length, not just first chunk
    )
    first_frame = header + first_chunk  # exactly 14 + 50 = 64 bytes
    assert len(first_frame) == FRAME_SIZE, (
        f"multi-frame first-frame length mismatch: {len(first_frame)}")
    frames: list[bytes] = [first_frame]
    # Continuation frames: each is 64 raw bytes of payload. The LAST
    # continuation frame carries the remaining payload + checksum + end
    # marker + zero-pad.
    rest = data[max_in_first_multi:]
    chk = xor_checksum(bytes(header[4:14]) + bytes(data))
    while rest:
        chunk = rest[:FRAME_SIZE]
        rest = rest[FRAME_SIZE:]
        if not rest:
            # LAST continuation: append checksum + end marker + zero-pad
            tail = bytes([chk, END_MARKER])
            chunk = chunk + tail
            if len(chunk) < FRAME_SIZE:
                chunk = chunk + b"\x00" * (FRAME_SIZE - len(chunk))
            elif len(chunk) > FRAME_SIZE:
                # Edge case: chk+end pushed us over — they belong in
                # an additional all-payload-was-here frame. Spill into
                # one more frame.
                spill_payload = chunk[:FRAME_SIZE]
                frames.append(spill_payload)
                chunk = bytes([chk, END_MARKER]) + b"\x00" * (FRAME_SIZE - 2)
        else:
            # Middle continuation: pure 64-byte payload chunk
            if len(chunk) < FRAME_SIZE:
                chunk = chunk + b"\x00" * (FRAME_SIZE - len(chunk))
        frames.append(chunk)
    return frames


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

    # Bytes actually present in this frame.
    #
    # Single-frame payloads: header (14) + payload (≤48) + chk (1) + end (1) = 64.
    # Multi-frame FIRST frames carry 50 payload bytes — no chk/end markers are
    # present in the first frame.  The chk/end go at the tail of the LAST
    # continuation frame (see build_frame / build_frames_multi).  The
    # ``payload_len`` field in the header declares the FULL logical payload
    # length, so any value > 48 means this is a multi-frame first frame and
    # this parse needs to extract 50 bytes, not 48.
    #
    # Historical note: an earlier version of this function capped ``present``
    # at 48 for all frames.  That silently dropped 2 bytes of the first frame
    # on every multi-frame READ (e.g. the 296-byte channel-state reply at
    # cmd=0x77NN), producing a reassembled blob where every byte from offset
    # 48 onward was 2 bytes left-shifted relative to the firmware's intent.
    # That bug was the root cause of the documented "read-divergence quirk"
    # and "firmware drops 2 bytes of multi-frame WRITE" phantoms that
    # pre-2026-04-22 versions of this library + its docs attributed to the
    # firmware.  Both were this parser under-reading by 2 bytes.  Confirmed
    # by comparing raw capture frames against dsp408.lua's reassembly (which
    # correctly uses 50 bytes for multi-frame first frames) — after the fix,
    # the Python library's reassembled blobs match the Wireshark dissector's
    # byte-for-byte and line up with Windows GUI capture payloads.
    is_multi_frame_first = payload_len > FRAME_SIZE - HEADER_SIZE - 2  # > 48
    if is_multi_frame_first:
        max_in_frame = FRAME_SIZE - HEADER_SIZE       # 50 — no chk/end in first
        max_available = len(raw) - HEADER_SIZE
    else:
        max_in_frame = FRAME_SIZE - HEADER_SIZE - 2   # 48 — chk+end at tail
        max_available = len(raw) - HEADER_SIZE - 2
    present = min(payload_len, max_in_frame, max_available)
    if present < 0:
        present = 0
    payload = bytes(raw[HEADER_SIZE : HEADER_SIZE + present])

    if is_multi_frame_first:
        # First frames of multi-frame payloads don't carry chk/end — they
        # live at the tail of the last continuation.  Mark checksum as
        # unverified here; multi-frame reassembly in transport.read_response
        # is responsible for validating the full reassembled payload.
        checksum = 0
        checksum_ok = False
    else:
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
CMD_STATE_0x13 = 0x13           # returns 10 bytes; meaning unknown — empirical
                                # probe (tests/loopback/_probe_state13.py) found
                                # the bytes are completely static across audio
                                # level / mute sweeps, so it's NOT a meter cmd.
CMD_STATUS = 0x34               # returns 1 byte (0x00 = idle)
CMD_GLOBAL_0x02 = 0x02          # returns 8 bytes: 01 00 01 00 00 00 00 00
CMD_GLOBAL_0x05 = 0x05          # returns 8 bytes: 28 00 00 32 00 32 01 00
CMD_GLOBAL_0x06 = 0x06          # returns 8 bytes: 03 09 04 0a 0f 12 16 17 (8-ch)

# Firmware commands (byte[7] = 0x09, direction = a1 WRITE)
CMD_FW_PREP = 0x36              # 16 zero bytes; triggers USB re-enumeration
CMD_FW_META = 0x37              # 4 bytes from .bin WMCU header offset 4
CMD_FW_BLOCK = 0x38             # 48 bytes of firmware per packet
CMD_FW_APPLY = 0x39             # 1 byte 0x13; applies and reboots

# Reset MCU / reboot trigger. Leon v1.23 Define.SYSTEM_RESET_MCU=96 (0x60);
# sendResetMUCData() (DataOptUtil.java:2725) emits DataType=9, ChannelID=96,
# DataID=0 with an 8-byte payload of the current wall-clock timestamp
# (sec/year_hi/year_lo/mon/day/hr/min/sec). Windows V1.24 sends 8 zero bytes
# instead. Device ACKs then disconnects from USB for ~11-14 s before
# re-enumerating. Seen at the end of captures/reset_to_defaults.pcapng,
# captures/windows-04-app-connect-and-settings.pcapng, and
# captures/windows-04b-volumes-mute-presets.pcapng — always right before the
# device silently goes away and comes back. Appears to be how the
# client-side GUI commits pending settings to flash and re-initialises.
CMD_RESET_MCU = 0x60

# Parameter commands (byte[7] = 0x04)
# Channel-level reads: cmd = (0x77 << 8) | channel_index (0..7), returns
# 296 bytes. The constant below is just the 0x77 nibble that gets shifted
# into the high byte by `device.read_channel_state()` — see that method
# for the actual cmd-build expression.
CMD_READ_CHANNEL_BASE = 0x0077
# Channel-level writes (0x1f00..0x1f07, one cmd per output channel).
# Payload is 8 bytes:
#     [0]  enable    (1 = audible, 0 = muted)
#     [1]  reserved  (always 0 in captures)
#     [2..3]  vol_le_u16     dB = (raw - 600) / 10  (0..600 = -60..0 dB)
#     [4..5]  delay_le_u16   samples (1 sample = 1 sample, exact). Firmware
#                            clamps at 359 samples — that's 8.143 ms @ 44.1 kHz
#                            (matches the manual's "8.1471 ms / 277 cm" max),
#                            but only 7.479 ms when the device is run at 48 kHz.
#                            The delay buffer is sized in taps, not in time.
#                            Verified live: tests/loopback/test_delay_calibration.py
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

CMD_WRITE_GLOBAL = 0x2000       # writes global params; the ONE captured use
                                # is the factory-reset magic word (cat=0x04
                                # payload=06 1f 00 00 20 4e 00 01) — see
                                # Device.factory_reset()

# Per-channel compressor write (0x2300..0x2307 = ch1..ch8). 8-byte payload
# decoded from captures/windows-04b-volumes-mute-presets.pcapng + verified
# live to land at blob[280..287]; per leon v1.23 source the field layout is:
#     [0..1]  all_pass_q_le16   (sidechain Q; firmware default 420)
#     [2..3]  attack_ms_le16    (firmware default 56)
#     [4..5]  release_ms_le16   (firmware default 500)
#     [6]     threshold         (units uncalibrated)
#     [7]     linkgroup_num     (channel link/group index, 0=no link)
# Note: per leon source there's NO enable bit in the wire format — the
# block is "always on" but inert in firmware v1.06 (live test confirms
# no audio change at any parameter combo, three-way confirmation:
# wire decode + firmware disasm + audio rig).
CMD_WRITE_COMPRESSOR_BASE = 0x2300

# Per-channel name write (0x2400..0x2407 = ch1..ch8). 8 bytes ASCII,
# zero-padded. Per leon v1.23 DataOptUtil.java:1138-1147 (DataID=36).
# Lands at blob[288..295] (default = 8 spaces + 0x00 trailing).
CMD_WRITE_CHANNEL_NAME_BASE = 0x2400

# Per-channel mixer SECOND HALF write (0x2200..0x2207 = ch1..ch8).
# 8 bytes for IN9..IN16 (mirror of CMD_ROUTING_BASE=0x21NN which holds
# IN1..IN8). Per leon v1.23 DataID=34. On DSP-408 hardware (max 4
# physical inputs) cells 9..16 are always zero, but writing them keeps
# us symmetric with the protocol — and forward-compatible with the
# sibling DSP-816 firmware.
CMD_ROUTING_HI_BASE = 0x2200

# ── Input-side processing (DataType=3 = MUSIC, cat=0x03) ────────────────
# Verified live 2026-04-19: cat=0x03 is wire-supported on USB. Reads
# return 288-byte input-state blobs; writes target individual subsystems
# via DataID encoding: cmd = (DataID << 8) | input_channel.
#
# Discovered DataID slots (live-verified blob landings):
#   DataID=0..8, 12..14: input EQ bands  (15 max per leon spec; live
#                                          stride is variable — bands 0..5
#                                          land in offsets 0..47 (8 bytes
#                                          each), then layout shifts; 30
#                                          bands of leon padding follow)
#   DataID=9          : input MISC (8-byte basic record at blob[70..77]):
#                          [feedback, polar, mode, mute, delay_le16,
#                           volume, spare]  (per leon source; field
#                           semantics not yet calibrated against audio)
#   DataID=10         : 8 bytes at blob[78..85] (subsystem unknown — not
#                          documented in leon source; possibly input
#                          compressor or a per-input limiter)
#   DataID=11         : input NOISEGATE (8 bytes at blob[86..93]):
#                          [threshold, attack, knee, release, config, ...]
#                          per leon source.
#   DataID=119 (0x77) : full input-channel state read (288 bytes)
CMD_READ_INPUT_BASE          = 0x0077  # cmd = (0x77 << 8) | input_ch
CMD_WRITE_INPUT_EQ_BAND_BASE = 0x0000  # cmd = (band << 8) | input_ch
CMD_WRITE_INPUT_MISC_BASE    = 0x0900  # cmd = 0x0900 | input_ch
CMD_WRITE_INPUT_DATAID10_BASE = 0x0A00 # cmd = 0x0A00 | input_ch (semantics TBD)
CMD_WRITE_INPUT_NOISEGATE_BASE = 0x0B00  # cmd = 0x0B00 | input_ch

INPUT_CHANNEL_COUNT = 8  # firmware exposes 8 input slots (DSP-408 hw has 4
                         # RCA + 4 high-level; cells 6+ may be aux/BT/unused)
INPUT_BLOB_SIZE = 288    # response length to cmd=0x77NN cat=0x03

# Full-channel-state write (296 bytes, output side). All live paths are on
# cat=0x04 (CAT_PARAM). Two parallel cmd encodings observed:
#   - DataID=119 (0x77), DataID<<8 | ch   = cmd 0x10000..0x10003 for ch 0..3
#                                           PLUS cmd 0x04..0x07 for ch 4..7
#     — emitted by the "Load preset from disk" flow in
#     captures/load_loaddisk_save_preset_bureau.pcapng (frames 2019..2103).
#   - DataID=0, ChannelID=ch              = cmd 0x00..0x07 for ch 0..7
#     — emitted by the V1.24 Windows GUI's "SaveGroupData" init sync in
#     captures/windows-04-app-connect-and-settings.pcapng (frames 6815..
#     6899) AND captures/full-sequence.pcapng. Byte-identical blob
#     contents between the two paths for the same channel (verified).
#
# Earlier notes incorrectly placed the cmd=0x04..0x07 variant on cat=0x09;
# re-verification across every capture shows only cat=0x04.
#
# Disambiguation:
#   cmd=0x04 cat=0x09 dir=a2 len=8   → CMD_GET_INFO (returns "MYDW-AV1.06")
#   cmd=0x04 cat=0x04 dir=a1 len=296 → full_channel_state write for ch=4
#   cmd=0x04 cat=0x04 dir=a2 len=8   → full_channel_state read for ch=4
CMD_WRITE_FULL_CHANNEL_LO_BASE = 0x10000  # ch 0..3 (DataID=0x77 path)
CMD_WRITE_FULL_CHANNEL_HI_BASE = 0x0004   # ch 4..7 (DataID=0x77 path, on cat=0x04)
CMD_FULL_CHANNEL_ALIAS_BASE    = 0x0000   # ch 0..7 (DataID=0 path, on cat=0x04 —
                                          # read + write both use this)

# Save preset trigger (per Windows preset-save capture): the GUI emits
# a dir=a1 WRITE to cmd=0x34 cat=0x09 with a single-byte 0x01 payload
# BEFORE the bulk-channel writes that commit state to internal flash.
# Without this trigger, the bulk writes appear to land in RAM only.
CMD_PRESET_SAVE_TRIGGER = 0x34
PRESET_SAVE_TRIGGER_BYTE = 0x01

# Per-channel HPF + LPF crossover writes (0x12000..0x12007 = ch1..ch8).
# 8-byte payload mirrors blob[256..263] exactly:
#     [0..1]  HPF freq  Hz LE16  (default 20)
#     [2]     HPF filter type    (0=BW, 1=Bessel, 2=LR, 3=?)
#     [3]     HPF slope          (0..7 = 6/12/18/24/30/36/42/48 dB/oct, 8=Off)
#     [4..5]  LPF freq  Hz LE16  (default 20000)
#     [6]     LPF filter type
#     [7]     LPF slope
# Verified live on hardware 2026-04-19 — surgical write, exact round-trip
# via blob readback at OFF_HPF_FREQ..OFF_LPF_SLOPE. Decoded from the
# windows-V1.24 GUI's filter-type-change writes in
# captures/full-sequence.pcapng.
CMD_WRITE_CROSSOVER_BASE = 0x12000

# Per-channel parametric-EQ band writes.
#     cmd = 0x10000 + (band << 8) + channel
#     band    = 0..9 (10 bands per channel; default centers 31/65/125/250/
#               500/1000/2000/4000/8000/16000 Hz)
#     channel = 0..7
# 8-byte payload (live-validated 2026-04-19, see
# tests/loopback/_probe_eq.py / docs/measurements/eq_band_q_sweep.md):
#     [0..1]  freq Hz LE16
#     [2..3]  gain raw LE16     dB = (raw - 600) / 10  (same as channel volume)
#     [4]     bandwidth byte    Q × bandwidth_byte ≈ 256   (peaking EQ)
#                               Higher = WIDER peak / LOWER Q.
#                               Default 0x34 = 52 → Q ≈ 4.9.
#     [5..7]  zeros
# Live measurement at fc=1000 Hz / +12 dB (pink-noise / Welch sweep):
#     b4=  8 → BW₃ ≈  59 Hz  → Q ≈ 17  (Q-resolution-limited)
#     b4= 26 → BW₃ ≈ 129 Hz  → Q ≈ 7.8
#     b4= 39 → BW₃ ≈ 170 Hz  → Q ≈ 5.9
#     b4= 52 → BW₃ ≈ 223 Hz  → Q ≈ 4.5  (firmware default)
#     b4= 78 → BW₃ ≈ 311 Hz  → Q ≈ 3.3
#     b4=104 → BW₃ ≈ 410 Hz  → Q ≈ 2.5
#     b4=208 → BW₃ ≈ 873 Hz  → Q ≈ 1.2
# b4·Q ranges 230..260 across b4∈[39, 156]; the asymptote at 256 (= 2⁸)
# strongly suggests the firmware encodes Q as Q ≈ 256/b4_byte using an
# 8-bit fixed-point reciprocal.
# The 296-byte variant (cmd=0x10000+ch with len=296) writes the entire
# channel state struct; appears to be how the GUI does "reset EQ to flat".
CMD_WRITE_EQ_BAND_BASE = 0x10000

# Number of parametric-EQ bands per output channel (10 — verified by
# decoding the channel-state blob at offsets 0..79).
EQ_BAND_COUNT = 10
# b4 ↔ Q relation (peaking EQ): Q ≈ EQ_Q_BW_CONSTANT / b4_byte.
# The constant is 256 (= 2⁸) — empirically fitted across b4 ∈ [39..156]
# at fc=1000 Hz / +12 dB to within ±5%. Asymptote suggests an 8-bit
# fixed-point reciprocal in the firmware.
EQ_Q_BW_CONSTANT = 256.0
# Default EQ band centers (read from a freshly-defaulted channel blob).
EQ_DEFAULT_FREQS_HZ = (31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

# Master payload constants
MASTER_LEVEL_MIN = 0     # raw = -60 dB
MASTER_LEVEL_MAX = 66    # raw = +6 dB
MASTER_LEVEL_OFFSET = 60  # raw = dB + 60

# Per-channel volume constants
CHANNEL_VOL_MIN = 0      # raw = -60 dB
CHANNEL_VOL_MAX = 600    # raw = 0 dB (unity)
CHANNEL_VOL_OFFSET = 600  # raw = (dB * 10) + 600

# Per-EQ-band gain range. Same encoding as channel volume (raw = dB*10 + 600)
# but EQ bands are allowed to BOOST as well as cut, so the upper cap is
# higher than CHANNEL_VOL_MAX. Live-verified at +12 dB / -60 dB only;
# the +60 dB ceiling is the protocol envelope, not a calibrated maximum.
EQ_GAIN_RAW_MIN  = 0      # = -60 dB
EQ_GAIN_RAW_MAX  = 1200   # = +60 dB (envelope; only +12 dB verified live)

# Subidx for each of the 8 channel write cmds. Order matches cmd index 0..7.
# These are also the *spk_type* (speaker-role) values stored at blob[255] of
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
# EQ region: bands 0..9 occupy offsets 0..79 (10 bands × 8 bytes), with
# the remaining bytes 80..245 holding leon-style padding / unused band
# slots. Live probe (tests/loopback/_probe_eq_extra_bands.py) confirmed
# that writes to band indices 10..30 are silently no-ops on this
# firmware: only 10 bands are functional even though the leon decompile
# showed 31 addressable slots.
BLOB_SIZE = 296

# Basic per-channel record (8 bytes at 248..255) — also the write payload format
# accepted by cmd=0x1FNN.
#
# Offsets on this page were corrected 2026-04-22 after the parse_frame fix:
# earlier versions of this library under-read the first frame of multi-frame
# replies by 2 bytes (reading 48 instead of 50 payload bytes), which made
# every blob position from firmware-offset 50 onward appear 2 bytes earlier
# in the reassembled blob.  The offsets below are the TRUE firmware-side
# positions, verified against the Windows GUI captures on the
# reverse-engineering branch (``captures/load_loaddisk_save_preset_bureau.pcapng``
# and ``captures/reset_to_defaults.pcapng``: 48/48 reassembled blobs show
# Q=420 at offset 280 and name-default spaces at 288..295).
OFF_MUTE        = 248  # 1=audible, 0=muted (INVERTED from leon's polarity)
OFF_POLAR       = 249  # phase invert: 0=normal, 1=inverted (180°)
OFF_GAIN        = 250  # u16 LE; raw = (dB * 10) + 600; range 0..600 = -60..0 dB
OFF_DELAY       = 252  # u16 LE; samples — exact 1:1, capped at 359 (firmware
                       # clamps; matches 8.14 ms @ 44.1 kHz, 7.48 ms @ 48 kHz).
                       # Empirical: tests/loopback/test_delay_calibration.py
OFF_BYTE_252    = 254  # Semantic unknown. Initially hypothesized to be the
                       # EQ enable/bypass flag (per the leon decompile's
                       # ``eq_mode`` field name) but live probe
                       # ``tests/loopback/_probe_eq_mode.py`` proved that
                       # writes to byte[6] of the channel write payload
                       # round-trip to blob[254] yet do NOT bypass EQ.
                       # Kept exposed under a neutral name; do NOT key
                       # automations on it.  (Name kept for backward compat
                       # with the old "252" label — value is now 254.)
OFF_EQ_MODE     = OFF_BYTE_252  # DEPRECATED alias — will be removed; the
                                # name is misleading (see OFF_BYTE_252).
OFF_SPK_TYPE    = 255  # speaker-role index; one of CHANNEL_SUBIDX by default

# Crossover (HPF + LPF) — 8 bytes at 256..263
OFF_HPF_FREQ    = 256  # u16 LE Hz (or table index)
OFF_HPF_FILTER  = 258  # 0=Butterworth, 1=Bessel, 2=Linkwitz-Riley
OFF_HPF_SLOPE   = 259  # 0..7 = 6/12/18/24/30/36/42/48 dB/oct, 8=Off
OFF_LPF_FREQ    = 260
OFF_LPF_FILTER  = 262
OFF_LPF_SLOPE   = 263

# Mixer — 8 bytes at 264..271 (one u8 percentage per input source IN1..IN8).
# leon's data model has 16 cells per output but our firmware exposes 8.
OFF_MIXER       = 264
MIXER_CELLS     = 8

# 8 bytes at 272..279 — semantic UNKNOWN. On Windows GUI captures this
# region mirrors the live compressor record (holding the same Q=420 /
# attack=56 / release=500 / threshold=0 / linkgroup=0 bytes as 280..287).
# On our spare device however, 272..279 reads as 8 × 0x20 (spaces) — the
# firmware default compressor values don't appear to be "pre-written" to
# the shadow unless the preset-load path has touched it.  Writes to
# cmd=0x2300+ch land at 280..287 (LIVE), not here, and we've never seen
# this region change in any live probe.  Best guess: a read-only
# "factory default" / GUI-populated shadow used by the (hidden)
# "Reset Compressor" button.
OFF_COMP_SHADOW = 272  # 8 bytes; do not interpret as live compressor

# Compressor / dynamics + link group — 8 bytes at 280..287 (LIVE; the
# location that cmd=0x2300+ch writes to and reads from).
OFF_ALL_PASS_Q  = 280  # u16 LE
OFF_ATTACK_MS   = 282  # u16 LE
OFF_RELEASE_MS  = 284  # u16 LE
OFF_THRESHOLD   = 286  # u8 (encoding TBD; likely dB-scaled)
OFF_LINKGROUP   = 287  # u8 channel link/group index (0 = no link)

# Per-channel name — 8 bytes ASCII at 288..295 (default: 8 × 0x20 spaces —
# or "7 spaces + 0x00" if the firmware NUL-terminates short names).  This
# is the last field in the 296-byte blob.
OFF_NAME        = 288
NAME_LEN        = 8

# Filter type / slope enums (for select-style controls in MQTT discovery).
FILTER_TYPE_BW = 0
FILTER_TYPE_BESSEL = 1
FILTER_TYPE_LR = 2
FILTER_TYPE_NAMES = (
    "Butterworth",       # 0
    "Bessel",            # 1
    "Linkwitz-Riley",    # 2
    "Linkwitz-Riley",    # 3 — verified live as a duplicate of type 2
                         #     (point-by-point max diff 0.21 dB across a
                         #     7-frequency sweep at fc=2 kHz / slope=24).
                         #     The Windows GUI's "Defeat" button writes
                         #     type=3 *and* moves freq to the band edge
                         #     (20 kHz LPF, 20 Hz HPF) — making the
                         #     filter inaudible by sleight of hand, NOT
                         #     true bypass. Real bypass is `slope=8`.
)
FILTER_TYPE_LR_ALIAS = 3

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
    "CMD_WRITE_COMPRESSOR_BASE",
    "CMD_WRITE_CHANNEL_NAME_BASE",
    "CMD_ROUTING_HI_BASE",
    "CMD_WRITE_CROSSOVER_BASE",
    "CMD_WRITE_EQ_BAND_BASE",
    "CMD_WRITE_FULL_CHANNEL_LO_BASE",
    "CMD_WRITE_FULL_CHANNEL_HI_BASE",
    "CMD_FULL_CHANNEL_ALIAS_BASE",
    "CMD_RESET_MCU",
    "CMD_PRESET_SAVE_TRIGGER",
    "PRESET_SAVE_TRIGGER_BYTE",
    "CAT_INPUT",
    "CMD_READ_INPUT_BASE",
    "CMD_WRITE_INPUT_EQ_BAND_BASE",
    "CMD_WRITE_INPUT_MISC_BASE",
    "CMD_WRITE_INPUT_DATAID10_BASE",
    "CMD_WRITE_INPUT_NOISEGATE_BASE",
    "INPUT_CHANNEL_COUNT",
    "INPUT_BLOB_SIZE",
    "FILTER_TYPE_LR_ALIAS",
    "EQ_BAND_COUNT",
    "EQ_Q_BW_CONSTANT",
    "EQ_DEFAULT_FREQS_HZ",
    "CMD_ROUTING_BASE",
    "CMD_MASTER",
    "MASTER_LEVEL_MIN",
    "MASTER_LEVEL_MAX",
    "MASTER_LEVEL_OFFSET",
    "CHANNEL_VOL_MIN",
    "CHANNEL_VOL_MAX",
    "CHANNEL_VOL_OFFSET",
    "EQ_GAIN_RAW_MIN",
    "EQ_GAIN_RAW_MAX",
    "CHANNEL_SUBIDX",
    "ROUTING_ON",
    "ROUTING_OFF",
    # blob layout
    "BLOB_SIZE",
    "OFF_MUTE", "OFF_POLAR", "OFF_GAIN", "OFF_DELAY",
    "OFF_BYTE_252", "OFF_EQ_MODE", "OFF_SPK_TYPE",
    "OFF_HPF_FREQ", "OFF_HPF_FILTER", "OFF_HPF_SLOPE",
    "OFF_LPF_FREQ", "OFF_LPF_FILTER", "OFF_LPF_SLOPE",
    "OFF_MIXER", "MIXER_CELLS",
    "OFF_COMP_SHADOW",
    "OFF_ALL_PASS_Q", "OFF_ATTACK_MS", "OFF_RELEASE_MS",
    "OFF_THRESHOLD", "OFF_LINKGROUP",
    "OFF_NAME", "NAME_LEN",
    # enums
    "FILTER_TYPE_BW", "FILTER_TYPE_BESSEL", "FILTER_TYPE_LR",
    "FILTER_TYPE_NAMES", "SLOPE_NAMES", "SPK_TYPE_NAMES",
]
