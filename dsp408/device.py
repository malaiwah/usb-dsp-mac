"""dsp408.device — high-level Device API for DSP-408 control.

Usage:

    from dsp408 import Device

    # Single-device (first found)
    with Device.open() as dev:
        dev.connect()
        print(dev.get_info())             # "MYDW-AV1.06"

    # Multiple devices
    for info in Device.enumerate():
        print(f"[{info['index']}] {info['display_id']}  path={info['path']!r}")

    # Select by index / serial / path
    with Device.open(selector=1) as dev: ...
    with Device.open(selector="MYDW-AV1234") as dev: ...
    with Device.open(path=b"/dev/hidraw0") as dev: ...

Scope note — what's implemented vs. TBD:

    Implemented (verified live in Windows captures):
      * connect(), get_info(), read_preset_name(), read_state_0x13(),
        read_status(), read_globals() (cmds 0x02 / 0x05 / 0x06)
      * read_channel_state(0..7)  — returns 296 raw bytes per channel
      * read_raw() / write_raw() escape hatches
      * Sequence counter that increments with every exchange
      * Multi-device enumeration + selection (serial / index / path)

    TBD (need live round-trip on the Pi to decode):
      * Parsing 0x77NN 296-byte channel state into EqBand / Crossover /
        Delay / Level typed fields
      * 0x1fNN sub-index → parameter name table (volume, mute, delay, …)
      * Mixer matrix read/write
      * Preset save/load/delete by slot
      * Streaming on/off toggle
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from .protocol import (
    CAT_PARAM,
    CAT_STATE,
    CMD_CONNECT,
    CMD_GET_INFO,
    CMD_IDLE_POLL,
    CMD_PRESET_NAME,
    CMD_READ_CHANNEL_BASE,
    CMD_STATUS,
    CMD_WRITE_CHANNEL_BASE,
    DIR_CMD,
    DIR_RESP,
    DIR_WRITE,
    DIR_WRITE_ACK,
    PID,
    VID,
    CMD_GLOBAL_0x02,
    CMD_GLOBAL_0x05,
    CMD_GLOBAL_0x06,
    CMD_STATE_0x13,
    Frame,
    build_frame,
)
from .transport import HidCompat, Transport


class DeviceNotFound(RuntimeError):
    """Raised when no DSP-408 is visible on the USB bus."""


class ProtocolError(RuntimeError):
    """Raised when the device replies with something unexpected."""


@dataclass(frozen=True)
class DeviceInfo:
    """Everything we learn from a fresh connect+info+preset_name round."""

    identity: str      # from cmd 0x04, e.g. "MYDW-AV1.06"
    preset_name: str   # from cmd 0x00
    status_byte: int   # from cmd 0x34
    global_02: bytes   # 8 bytes from cmd 0x02
    global_05: bytes   # 8 bytes from cmd 0x05
    global_06: bytes   # 8 bytes from cmd 0x06
    state_13: bytes    # 10 bytes from cmd 0x13


# ── enumeration helpers ──────────────────────────────────────────────────
def _path_hash(path: bytes) -> str:
    """Short stable hash of an hidapi path, for use in display_id when
    serial is missing or duplicated."""
    return hashlib.sha1(path or b"").hexdigest()[:8]


def _build_display_id(info: dict, index: int, serial_counts: dict) -> str:
    """Pick a stable string identifier for a device.

    Preference order:
      1. Non-empty serial number if unique across the bus  → "MYDW-AV1234"
      2. Serial + index (when multiple devices share a serial) → "MYDW-AV1234#1"
      3. Path hash fallback (VID/PID-based USB paths with no serial) → "dsp408-a1b2c3d4"

    The result is always a valid MQTT topic component and a valid HA
    unique_id suffix.
    """
    serial = (info.get("serial_number") or "").strip()
    if serial and serial_counts.get(serial, 0) == 1:
        return serial
    if serial:
        return f"{serial}#{index}"
    path = info.get("path") or b""
    return f"dsp408-{_path_hash(path)}"


def enumerate_devices() -> list[dict]:
    """Return enriched info dicts for every DSP-408 on the bus.

    Each entry has: index, vid, pid, path (bytes), serial_number,
    product_string, manufacturer, display_id.
    """
    raw = HidCompat.enumerate(VID, PID)
    # Deduplicate by path (hidapi on Linux can report the same hidraw
    # node once per HID usage page).
    seen: set[bytes] = set()
    uniq: list[dict] = []
    for d in raw:
        p = d.get("path") or b""
        if p in seen:
            continue
        seen.add(p)
        uniq.append(d)

    # Count serials so we know if any are duplicated.
    serial_counts: dict[str, int] = {}
    for d in uniq:
        s = (d.get("serial_number") or "").strip()
        if s:
            serial_counts[s] = serial_counts.get(s, 0) + 1

    out: list[dict] = []
    for idx, d in enumerate(uniq):
        out.append(
            {
                "index": idx,
                "vid": d.get("vendor_id", VID),
                "pid": d.get("product_id", PID),
                "path": d.get("path") or b"",
                "serial_number": (d.get("serial_number") or "").strip(),
                "product_string": (d.get("product_string") or "").strip(),
                "manufacturer": (d.get("manufacturer_string") or "").strip(),
                "display_id": _build_display_id(d, idx, serial_counts),
            }
        )
    return out


def resolve_selector(
    selector: int | str | None,
    devs: list[dict],
) -> dict:
    """Pick one device-info dict from the enumerated list.

    Public API for CLI / UI code that needs to resolve a user-provided
    selector (index, serial, display_id, or path) against an enumerated
    device list.
    """
    return _resolve_selector(selector, devs)


def _resolve_selector(
    selector: int | str | None,
    devs: list[dict],
) -> dict:
    """Pick one device-info dict from the enumerated list."""
    if not devs:
        raise DeviceNotFound(
            f"No DSP-408 found (VID={VID:#06x} PID={PID:#06x})"
        )
    if selector is None:
        return devs[0]
    # Integer or int-like string: treat as index.
    if isinstance(selector, int):
        if not 0 <= selector < len(devs):
            raise DeviceNotFound(
                f"Device index {selector} out of range (have {len(devs)})"
            )
        return devs[selector]
    if isinstance(selector, str):
        s = selector.strip()
        if s.isdigit():
            return _resolve_selector(int(s), devs)
        # Match display_id, then serial, then path as string.
        for d in devs:
            if d["display_id"] == s:
                return d
        for d in devs:
            if d["serial_number"] and d["serial_number"] == s:
                return d
        for d in devs:
            try:
                if d["path"].decode(errors="replace") == s:
                    return d
            except Exception:
                pass
        raise DeviceNotFound(
            f"No DSP-408 matches selector {selector!r}. "
            f"Available: {[d['display_id'] for d in devs]}"
        )
    raise TypeError(f"selector must be int|str|None, got {type(selector)}")


class Device:
    """High-level DSP-408 USB control.

    Not thread-safe across instances (the device is a single serialized
    endpoint); internally serializes commands via a lock so that CLI and
    Gradio UI threads can share one Device.
    """

    def __init__(self, transport: Transport, info: dict | None = None):
        self._t = transport
        self._seq = 0
        self._lock = threading.Lock()
        self._info: DeviceInfo | None = None
        # Enumeration info at open time (path, serial, display_id).
        self._enum_info: dict = info or {}

    # ── enumeration / opening ──────────────────────────────────────────
    @staticmethod
    def enumerate() -> list[dict]:
        """Return enriched info dicts for every DSP-408 on the bus.

        Each entry has: index, vid, pid, path (bytes), serial_number,
        product_string, manufacturer, display_id.
        """
        return enumerate_devices()

    @classmethod
    def open(
        cls,
        selector: int | str | None = None,
        *,
        path: bytes | None = None,
    ) -> Device:
        """Open one DSP-408.

        Args:
            selector: None → first found; int → index into enumerate();
                str → display_id, serial number, or string-encoded path.
            path: explicit hidapi path (bytes); takes precedence over selector.
        """
        devs = enumerate_devices()
        if path is not None:
            chosen = next((d for d in devs if d["path"] == path), None)
            if chosen is None:
                # Allow opening by raw path even if not in enumerate list
                # (useful if udev is slow to update).
                chosen = {"path": path, "display_id": f"dsp408-{_path_hash(path)}",
                          "serial_number": "", "product_string": "",
                          "manufacturer": "", "index": -1,
                          "vid": VID, "pid": PID}
        else:
            chosen = _resolve_selector(selector, devs)
        hid_conn = HidCompat().open_path(chosen["path"])
        return cls(Transport(hid_conn), info=chosen)

    def close(self) -> None:
        # Acquire the exchange lock so a concurrent _exchange() on another
        # thread can't race us: it holds _lock during every read/write, so
        # waiting for it guarantees no in-flight I/O when we drop _t.
        with self._lock:
            if self._t is not None:
                try:
                    self._t.hid.close()
                finally:
                    self._t = None  # type: ignore[assignment]

    def __enter__(self) -> Device:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── identity ───────────────────────────────────────────────────────
    @property
    def display_id(self) -> str:
        """Stable string identifier suitable for MQTT topics / HA unique_id."""
        return self._enum_info.get("display_id") or "dsp408"

    @property
    def serial_number(self) -> str:
        return self._enum_info.get("serial_number") or ""

    @property
    def path(self) -> bytes:
        return self._enum_info.get("path") or b""

    @property
    def enum_info(self) -> dict:
        """Read-only copy of the enumeration dict (index/serial/path/etc.)."""
        return dict(self._enum_info)

    # ── low-level exchange ─────────────────────────────────────────────
    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return s

    def _exchange(
        self,
        direction: int,
        cmd: int,
        data: bytes = b"\x00" * 8,
        category: int = CAT_STATE,
        timeout_ms: int = 2000,
        expect_reply: bool = True,
    ) -> Frame | None:
        """Send one frame, optionally wait for the matching reply.

        If a previous exchange timed out and the device later emits its
        late reply, we may see a stale frame here (different cmd).
        Rather than bail out immediately, keep draining until we find
        the right cmd or the overall deadline expires.
        """
        if self._t is None:
            raise ProtocolError("device is closed")
        with self._lock:
            seq = self._next_seq()
            frame = build_frame(
                direction=direction,
                seq=seq,
                cmd=cmd,
                data=data,
                category=category,
            )
            self._t.send_frame(frame)
            if not expect_reply:
                return None
            deadline = time.monotonic() + timeout_ms / 1000.0
            while True:
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise ProtocolError(
                        f"No reply to cmd=0x{cmd:02x} cat=0x{category:02x} "
                        f"seq={seq} (timeout {timeout_ms} ms)"
                    )
                reply = self._t.read_response(timeout_ms=remaining_ms)
                if reply is None:
                    raise ProtocolError(
                        f"No reply to cmd=0x{cmd:02x} cat=0x{category:02x} "
                        f"seq={seq} (timeout {timeout_ms} ms)"
                    )
                # Lenient seq match: device sometimes returns seq=0 regardless.
                if reply.cmd == cmd:
                    return reply
                # Stale frame from a previous exchange — skip and retry.
                continue

    # ── public escape hatches ──────────────────────────────────────────
    def read_raw(
        self,
        cmd: int,
        data: bytes = b"\x00" * 8,
        category: int = CAT_STATE,
        timeout_ms: int = 2000,
    ) -> Frame:
        """Issue a READ (dir=a2) and return the device's reply Frame."""
        reply = self._exchange(
            direction=DIR_CMD,
            cmd=cmd,
            data=data,
            category=category,
            timeout_ms=timeout_ms,
        )
        assert reply is not None
        if reply.direction != DIR_RESP:
            raise ProtocolError(
                f"expected READ reply (0x{DIR_RESP:02x}), got 0x{reply.direction:02x}"
            )
        return reply

    def write_raw(
        self,
        cmd: int,
        data: bytes,
        category: int = CAT_PARAM,
        timeout_ms: int = 2000,
    ) -> Frame:
        """Issue a WRITE (dir=a1) and return the device's ack Frame."""
        reply = self._exchange(
            direction=DIR_WRITE,
            cmd=cmd,
            data=data,
            category=category,
            timeout_ms=timeout_ms,
        )
        assert reply is not None
        if reply.direction != DIR_WRITE_ACK:
            raise ProtocolError(
                f"expected WRITE ack (0x{DIR_WRITE_ACK:02x}), "
                f"got 0x{reply.direction:02x}"
            )
        return reply

    # ── proven commands ────────────────────────────────────────────────
    def connect(self) -> int:
        """Open the command session. Returns the 1-byte status code the
        device replies with (0x00 = ok)."""
        reply = self.read_raw(cmd=CMD_CONNECT, category=CAT_STATE)
        if not reply.payload:
            raise ProtocolError("CONNECT: empty payload")
        return reply.payload[0]

    def get_info(self) -> str:
        """Return the device identity string, e.g. `"MYDW-AV1.06"`."""
        reply = self.read_raw(cmd=CMD_GET_INFO, category=CAT_STATE)
        return reply.payload.rstrip(b"\x00").decode("ascii", errors="replace")

    def read_preset_name(self) -> str:
        """Read the active preset's user-assigned name."""
        reply = self.read_raw(cmd=CMD_PRESET_NAME, category=CAT_STATE)
        return reply.payload.rstrip(b"\x00").decode("ascii", errors="replace")

    def write_preset_name(self, name: str) -> None:
        """Rename the active preset (up to 15 chars)."""
        payload = name.encode("ascii")[:15].ljust(16, b"\x00")
        self.write_raw(cmd=CMD_PRESET_NAME, data=payload, category=CAT_STATE)

    def read_status(self) -> int:
        reply = self.read_raw(cmd=CMD_STATUS, category=CAT_STATE)
        return reply.payload[0] if reply.payload else 0

    def read_state_0x13(self) -> bytes:
        """Read the 10-byte state blob (meaning TBD, possibly meter levels)."""
        reply = self.read_raw(cmd=CMD_STATE_0x13, category=CAT_STATE)
        return reply.payload

    def read_globals(self) -> tuple[bytes, bytes, bytes]:
        """Read the three 8-byte global blobs seen at session startup.

        Returns (cmd02, cmd05, cmd06). Layouts TBD; known examples from
        windows-01-fw-update-original-V6.21.pcapng:
            cmd02 = 01 00 01 00 00 00 00 00
            cmd05 = 28 00 00 32 00 32 01 00
            cmd06 = 03 09 04 0a 0f 12 16 17  (looks per-channel indexed)
        """
        r02 = self.read_raw(cmd=CMD_GLOBAL_0x02, category=CAT_STATE).payload
        r05 = self.read_raw(cmd=CMD_GLOBAL_0x05, category=CAT_STATE).payload
        r06 = self.read_raw(cmd=CMD_GLOBAL_0x06, category=CAT_STATE).payload
        return r02, r05, r06

    def idle_poll(self) -> bytes:
        """The cmd the official app emits every ~30 ms to keep the session
        alive. Returns the 15-byte preset-name blob just like cmd 0x00."""
        reply = self.read_raw(cmd=CMD_IDLE_POLL, category=CAT_STATE)
        return reply.payload

    # ── parameter-level reads ──────────────────────────────────────────
    def read_channel_state(self, channel: int) -> bytes:
        """Read the full state of output channel N (0..7) — 296 bytes.

        Layout is not yet decoded. Known prefix from one capture:
            28 01 1f 00 | 58 02 34 00 00 00 | 41 00 ...
        The capture header encodes (40+1, 31, 0) as LE u16 — possibly a
        structure size/version/count triplet — but this is unconfirmed.
        Use the raw bytes and decode live on the Pi.
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        cmd = CMD_READ_CHANNEL_BASE | (channel << 8)   # 0x7700, 0x7701, …
        reply = self.read_raw(cmd=cmd, category=CAT_PARAM, timeout_ms=3000)
        return reply.payload

    def write_channel_param(
        self,
        channel: int,
        value: int,
        sub_index: int,
    ) -> None:
        """Write a single channel parameter.

        Payload layout observed in windows-04c-stream-nostream-stream:
            01 00 | value_le_u32 | 00 | sub_index

        Sub-index → parameter mapping (incomplete — needs live validation):
            0x1f02/0x03, 0x1f03/0x07, 0x1f04/0x08, 0x1f05/0x09,
            0x1f06/0x0f, 0x1f07/0x12. Likely one sub-index per parameter
            type (volume/mute/delay/phase/hpf/lpf/band1/band2/...).
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("value must fit in u32")
        cmd = CMD_WRITE_CHANNEL_BASE | (channel << 8)  # 0x1f00..0x1f07
        payload = (
            b"\x01\x00"
            + value.to_bytes(4, "little")
            + b"\x00"
            + bytes([sub_index & 0xFF])
        )
        self.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)

    # ── one-shot snapshot ──────────────────────────────────────────────
    def snapshot(self) -> DeviceInfo:
        """Run the handshake sequence the GUI runs at startup and cache."""
        self.connect()
        identity = self.get_info()
        preset_name = self.read_preset_name()
        status_byte = self.read_status()
        state_13 = self.read_state_0x13()
        g02, g05, g06 = self.read_globals()
        self._info = DeviceInfo(
            identity=identity,
            preset_name=preset_name,
            status_byte=status_byte,
            global_02=g02,
            global_05=g05,
            global_06=g06,
            state_13=state_13,
        )
        return self._info

    @property
    def cached_info(self) -> DeviceInfo | None:
        return self._info
