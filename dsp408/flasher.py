"""dsp408.flasher — DSP-408 firmware upload over USB HID.

Implements the upload sequence proven out in the Windows capture
`captures/windows-01-fw-update-original-V6.21.pcapng`:

    1.  65× 64-byte frames of 0x43 0x11 repeating  — trigger firmware mode
    2.  cmd=0x36 (dir=a1): 16 zero bytes           — prepare (re-enumerates)
    3.  reopen USB device after re-enumeration
    4.  cmd=0x37 (dir=a1): 4 bytes from WMCU header offset 4 — metadata
    5.  cmd=0x38 (dir=a1) × N: 48-byte firmware blocks (data from .bin+8)
    6.  cmd=0x39 (dir=a1): 1 byte 0x13              — apply + reboot

.bin file format (8-byte prefix + raw image):
    offset 0: "WMCU"
    offset 4: 4-byte metadata (sent in cmd=0x37)
    offset 8..: firmware image (uploaded in 48-byte chunks)
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .protocol import (
    CAT_STATE,
    CMD_CONNECT,
    CMD_FW_APPLY,
    CMD_FW_BLOCK,
    CMD_FW_META,
    CMD_FW_PREP,
    DIR_CMD,
    DIR_WRITE,
    DIR_WRITE_ACK,
    FRAME_SIZE,
    build_frame,
)
from .transport import HidCompat, Transport

# ── constants ──────────────────────────────────────────────────────────
FW_HEADER_SIZE = 8           # "WMCU" 8-byte prefix
BLOCK_SIZE = 48              # firmware bytes per cmd=0x38 frame
FW_TRIGGER_FRAMES = 65       # 0x43 0x11 frames needed to enter fw mode
FW_TRIGGER_PATTERN = bytes([0x43, 0x11] * 32)  # exactly 64 bytes


def _connect_cmd_frame(seq: int = 0) -> bytes:
    """Build the cmd=0xcc CONNECT frame (sent before firmware mode)."""
    return build_frame(
        direction=DIR_CMD,
        seq=seq,
        cmd=CMD_CONNECT,
        data=b"\x00" * 8,
        category=CAT_STATE,
    )


class FirmwareError(RuntimeError):
    pass


def flash_firmware(
    fw_path: Path,
    progress: Callable[[int, int, str], None] | None = None,
    device_path: bytes | None = None,
) -> None:
    """Flash a .bin firmware image onto a DSP-408.

    Args:
        fw_path: path to .bin (must start with "WMCU").
        progress: optional callback(current, total, phase_label). Called
            repeatedly during block upload; `total` is the block count.
        device_path: hidapi path to target a specific DSP-408 when more
            than one is attached. Defaults to the first found.

    Raises:
        FileNotFoundError, FirmwareError.
    """
    fw_path = Path(fw_path)
    raw = fw_path.read_bytes()
    if raw[:4] != b"WMCU":
        raise FirmwareError(f"{fw_path}: missing WMCU header")

    meta = raw[4:8]
    fw_data = raw[FW_HEADER_SIZE:]
    total_bytes = len(fw_data)
    blocks = (total_bytes + BLOCK_SIZE - 1) // BLOCK_SIZE

    def _report(cur: int, total: int, label: str) -> None:
        if progress:
            progress(cur, total, label)

    # ── step 0: find device ───────────────────────────────────────────
    if device_path is not None:
        path = device_path
    else:
        devs = HidCompat.enumerate(0x0483, 0x5750)
        if not devs:
            raise FirmwareError("DSP-408 not found on USB bus")
        path = devs[0]["path"]

    hid = HidCompat().open_path(path)
    t = Transport(hid)
    _report(0, blocks, "connect")

    # ── step 1: CONNECT (best-effort; tolerates already-in-fw state) ──
    t.send_frame(_connect_cmd_frame(seq=0))
    t.read_frame(timeout_ms=500)   # reply optional

    # ── step 2: enter firmware mode ───────────────────────────────────
    _report(0, blocks, "trigger firmware mode")
    for _ in range(FW_TRIGGER_FRAMES):
        hid.write(b"\x00" + FW_TRIGGER_PATTERN)
        hid.read(FRAME_SIZE, 100)  # drain

    # ── step 3: PREP (triggers USB re-enumeration) ────────────────────
    _report(0, blocks, "prepare (re-enum)")
    prep = build_frame(
        direction=DIR_WRITE,
        seq=0,
        cmd=CMD_FW_PREP,
        data=b"\x00" * 16,
        category=CAT_STATE,
    )
    t.send_frame(prep)
    ack = t.read_frame(timeout_ms=2000)
    hid.close()
    if ack is None or ack.direction != DIR_WRITE_ACK:
        raise FirmwareError("cmd=0x36 (prep) did not ack")

    # ── step 4: reopen after re-enumeration ───────────────────────────
    hid, t = _reopen(path)

    # ── step 5: metadata ──────────────────────────────────────────────
    _report(0, blocks, "metadata")
    meta_frame = build_frame(
        direction=DIR_WRITE,
        seq=0,
        cmd=CMD_FW_META,
        data=meta,
        category=CAT_STATE,
    )
    t.send_frame(meta_frame)
    ack = t.read_frame(timeout_ms=2000)
    if ack is None or ack.direction != DIR_WRITE_ACK:
        hid.close()
        raise FirmwareError("cmd=0x37 (meta) did not ack")

    # ── step 6: upload blocks ─────────────────────────────────────────
    _report(0, blocks, "upload")
    for i in range(blocks):
        chunk = fw_data[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
        chunk = chunk.ljust(BLOCK_SIZE, b"\x00")
        frm = build_frame(
            direction=DIR_WRITE,
            seq=i & 0xFF,
            cmd=CMD_FW_BLOCK,
            data=chunk,
            category=CAT_STATE,
        )
        t.send_frame(frm)
        ack = t.read_frame(timeout_ms=2000)
        if ack is None or ack.direction != DIR_WRITE_ACK:
            hid.close()
            raise FirmwareError(f"no ack for block {i}/{blocks}")
        _report(i + 1, blocks, "upload")

    # ── step 7: apply + reboot ────────────────────────────────────────
    _report(blocks, blocks, "apply")
    apply_frame = build_frame(
        direction=DIR_WRITE,
        seq=0,
        cmd=CMD_FW_APPLY,
        data=bytes([0x13]),
        category=CAT_STATE,
    )
    t.send_frame(apply_frame)
    # Device reboots — reply is optional.
    t.read_frame(timeout_ms=1000)
    hid.close()
    _report(blocks, blocks, "done (device rebooting ~20 s)")


def _reopen(path: bytes) -> tuple[HidCompat, Transport]:
    """Wait for USB re-enumeration after cmd=0x36, then reopen.

    Must tolerate the device not being visible yet. hidapi raises different
    exception classes depending on which flavor is installed: legacy
    `cython-hidapi` raises OSError/IOError, the newer `hid.Device` raises
    `hid.HIDException`. Catch broadly on re-open only.
    """
    last_err: BaseException | None = None
    for _ in range(30):
        time.sleep(0.5)
        try:
            h = HidCompat().open_path(path)
            return h, Transport(h)
        except Exception as e:  # noqa: BLE001 — deliberately broad on re-open
            last_err = e
    raise FirmwareError(
        f"device did not re-enumerate within 15 s: {last_err}"
    )
