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

    Implemented (verified live on hardware via the Scarlett loopback rig
    in tests/loopback/ and/or against Windows USBPcap captures):
      * connect(), get_info(), read_preset_name(), read_status(),
        read_globals() (cmds 0x02 / 0x05 / 0x06), read_channel_state(0..7)
        + parse_channel_state_blob()
      * Master volume / mute (set_master*, get_master)
      * Per-channel volume / mute / delay / phase invert (set_channel)
      * Routing matrix (8x4 u8 cells) — set_routing / set_routing_cell
      * Crossover HPF + LPF — set_crossover (filter type + slope, 6..48
        dB/oct + bypass)
      * 10-band parametric EQ — set_eq_band (q OR bandwidth_byte)
      * Magic-word system register stubs — factory_reset / load_preset
        (KNOWN-BROKEN: wire encoding still unverified — see method docs)
      * Multi-device enumeration + selection (serial / index / path)
      * read_raw() / write_raw() escape hatches

    Known unknowns (decode pending — see /tmp/dsp408-re/notes/
    captures-needed-from-windows.md on the reverse-engineering branch):
      * Live VU meters (cmd=0x13 and idle-poll cmd=0x03 both proved
        static; meter cmd unknown — capture #8 needed)
      * Per-channel name (set / encoding — capture #1 needed)
      * Compressor write API (cmd=0x2300+ch decoded but never driven
        end-to-end on the rig — see set_compressor())
      * Preset save/load/delete by slot (capture #3 needed)
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from .config import friendly_name_for, load_aliases
from .protocol import (
    CAT_PARAM,
    CAT_STATE,
    CHANNEL_SUBIDX,
    CHANNEL_VOL_MAX,
    CHANNEL_VOL_MIN,
    CHANNEL_VOL_OFFSET,
    EQ_GAIN_RAW_MAX,
    EQ_GAIN_RAW_MIN,
    CMD_CONNECT,
    CMD_GET_INFO,
    CMD_IDLE_POLL,
    CMD_MASTER,
    CMD_PRESET_NAME,
    CMD_WRITE_GLOBAL,
    CMD_READ_CHANNEL_BASE,
    CMD_ROUTING_BASE,
    CMD_STATUS,
    CMD_WRITE_CHANNEL_BASE,
    CMD_WRITE_COMPRESSOR_BASE,
    CMD_WRITE_CROSSOVER_BASE,
    CMD_WRITE_EQ_BAND_BASE,
    EQ_BAND_COUNT,
    EQ_DEFAULT_FREQS_HZ,
    EQ_Q_BW_CONSTANT,
    DIR_CMD,
    DIR_RESP,
    DIR_WRITE,
    DIR_WRITE_ACK,
    MASTER_LEVEL_MAX,
    MASTER_LEVEL_MIN,
    MASTER_LEVEL_OFFSET,
    MIXER_CELLS,
    NAME_LEN,
    OFF_ALL_PASS_Q,
    OFF_ATTACK_MS,
    OFF_DELAY,
    OFF_BYTE_252,
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
    PID,
    ROUTING_OFF,
    ROUTING_ON,
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


def enumerate_devices(aliases: dict[str, str] | None = None) -> list[dict]:
    """Return enriched info dicts for every DSP-408 on the bus.

    Each entry has: index, vid, pid, path (bytes), serial_number,
    product_string, manufacturer, display_id, friendly_name.

    `friendly_name` is the alias from the user's config (if any matches
    the device's serial / display_id / path), otherwise equal to
    display_id. Callers can supply an explicit `aliases` dict (e.g. from
    a `--aliases PATH` CLI flag); if None, the default search paths are
    used (see dsp408.config.default_search_paths).
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

    if aliases is None:
        aliases = load_aliases()

    out: list[dict] = []
    for idx, d in enumerate(uniq):
        info = {
            "index": idx,
            "vid": d.get("vendor_id", VID),
            "pid": d.get("product_id", PID),
            "path": d.get("path") or b"",
            "serial_number": (d.get("serial_number") or "").strip(),
            "product_string": (d.get("product_string") or "").strip(),
            "manufacturer": (d.get("manufacturer_string") or "").strip(),
            "display_id": _build_display_id(d, idx, serial_counts),
        }
        info["friendly_name"] = friendly_name_for(info, aliases) or info["display_id"]
        out.append(info)
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
        # Match friendly_name, then display_id, then serial, then path string.
        for d in devs:
            if d.get("friendly_name") and d["friendly_name"] == s:
                return d
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
        available = [
            d.get("friendly_name") or d["display_id"] for d in devs
        ]
        raise DeviceNotFound(
            f"No DSP-408 matches selector {selector!r}. "
            f"Available: {available}"
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
    def friendly_name(self) -> str:
        """User-facing name: alias from config if set, else display_id.

        Safe for UI labels. For MQTT topics / unique IDs, prefer `display_id`.
        """
        return self._enum_info.get("friendly_name") or self.display_id

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
            # WRITES (dir=a1) MUST use seq=0 — the device silently drops
            # writes with non-zero seq on cat=0x04 cmds (per-channel volume,
            # routing matrix, EQ). The Windows GUI uses seq=0 for every
            # write and only increments seq for READ requests. Reads keep
            # the auto-increment so we can still match late replies to the
            # correct in-flight request.
            seq = 0 if direction == DIR_WRITE else self._next_seq()
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

        Layout is partially decoded.  Known prefix from one capture:
            28 01 1f 00 | 58 02 34 00 00 00 | 41 00 ...
        where bytes[4:6] as LE16 = 600 (volume at 0 dB), bytes[6:8] as
        LE16 = 52 (delay in samples).

        The blob also embeds the 8-byte write-format channel record at
        offset ~246:
            [en, 00, vol_lo, vol_hi, delay_lo, delay_hi, 00, subidx]
        Use `parse_channel_state_blob()` to extract that record.
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        # CMD_READ_CHANNEL_BASE is 0x0077; reads in capture are 0x7700..0x7707.
        # Channel index goes in the high byte: 0x77NN → ((0x77 << 8) | NN).
        # (Historically this method built `0x77 | (NN<<8)` which silently
        # produced wrong cmds for channel > 0 — fixed below.)
        cmd = (CMD_READ_CHANNEL_BASE << 8) | channel    # 0x7700, 0x7701, …
        reply = self.read_raw(cmd=cmd, category=CAT_PARAM, timeout_ms=3000)
        return reply.payload

    @staticmethod
    def parse_channel_state_blob(blob: bytes, channel: int) -> dict | None:
        """Decode the full 296-byte per-channel state blob into a flat dict.

        The blob returned by ``cmd=0x77NN`` (read_channel_state) is the
        firmware's per-channel struct in RAM.  Field offsets in the second
        half (246..285) were confirmed live on real DSP-408 hardware and
        align with the official Android leon v1.23 app's
        ``DataStruct_Output`` layout shifted by 2 bytes.

        Layout (verified offsets — see protocol.py for symbolic names):

        .. code-block:: text

            0..79    parametric-EQ bands 0..9 (10 × 8-byte records)
            80..245  unused / leon-style padding for the bands the GUI
                     never exposes (writes to bands 10..30 silently no-op)
            246      mute       1=audible, 0=muted (NOTE: inverted vs leon)
            247      polar      0=normal, 1=phase-inverted (180°)
            248-249  gain_le16  raw = (dB×10)+600; range 0..600 = -60..0 dB
            250-251  delay_le16 samples (or cm-step index)
            252      byte_252   semantic unknown — leon called it `eq_mode`
                                but live probe proved writes here do NOT
                                bypass EQ. Round-trips correctly; do not
                                key automations on it.
            253      spk_type   speaker-role index; default in CHANNEL_SUBIDX
            254-255  hpf_freq_le16
            256      hpf_filter (0=BW, 1=Bessel, 2=LR)
            257      hpf_slope  (0..7 = 6/12/18/24/30/36/42/48 dB/oct, 8=Off)
            258-259  lpf_freq_le16
            260      lpf_filter
            261      lpf_slope
            262-269  mixer IN1..IN8 (8 × u8 percentage)
            270-277  comp_shadow      (semantic unknown — reads identical
                                       to the live compressor record but
                                       does NOT track writes; see protocol)
            278-279  all_pass_q_le16
            280-281  attack_ms_le16   (compressor attack)
            282-283  release_ms_le16  (compressor release)
            284      threshold        (compressor)
            285      linkgroup        (channel link/group index, 0=none)
            286-293  name (8-byte ASCII channel label)

        Returns the legacy keys (``db``, ``muted``, ``delay``, ``subidx``)
        plus the new keys (``polar``, ``byte_252``, ``spk_type``, ``hpf``,
        ``lpf``, ``mixer``, ``compressor``, ``linkgroup``, ``name``,
        ``raw``).  ``byte_252`` is the raw value of blob[252] — semantic
        unknown (was previously called ``eq_mode``; see protocol.py).

        ``hpf``/``lpf`` are sub-dicts ``{"freq", "filter", "slope"}``.
        ``compressor`` is ``{"attack_ms", "release_ms", "threshold",
        "all_pass_q"}``.

        Returns ``None`` if the blob is too short or ``mute``/``gain`` are
        out of range (corrupt blob).
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        # Need through offset 285 (last byte of name field).
        if len(blob) < OFF_NAME + NAME_LEN:
            return None

        # Sanity-check the basic record fields before trusting anything else.
        en_bit = blob[OFF_MUTE]
        if en_bit not in (0, 1):
            return None  # corrupt blob

        raw_vol = int.from_bytes(blob[OFF_GAIN:OFF_GAIN + 2], "little")
        if not (0 <= raw_vol <= CHANNEL_VOL_MAX):
            return None  # corrupt blob

        polar = blob[OFF_POLAR]
        raw_delay = int.from_bytes(blob[OFF_DELAY:OFF_DELAY + 2], "little")
        byte_252 = blob[OFF_BYTE_252]
        spk_type = blob[OFF_SPK_TYPE]

        hpf = {
            "freq": int.from_bytes(
                blob[OFF_HPF_FREQ:OFF_HPF_FREQ + 2], "little"),
            "filter": blob[OFF_HPF_FILTER],
            "slope": blob[OFF_HPF_SLOPE],
        }
        lpf = {
            "freq": int.from_bytes(
                blob[OFF_LPF_FREQ:OFF_LPF_FREQ + 2], "little"),
            "filter": blob[OFF_LPF_FILTER],
            "slope": blob[OFF_LPF_SLOPE],
        }
        mixer = list(blob[OFF_MIXER:OFF_MIXER + MIXER_CELLS])
        compressor = {
            "all_pass_q": int.from_bytes(
                blob[OFF_ALL_PASS_Q:OFF_ALL_PASS_Q + 2], "little"),
            "attack_ms": int.from_bytes(
                blob[OFF_ATTACK_MS:OFF_ATTACK_MS + 2], "little"),
            "release_ms": int.from_bytes(
                blob[OFF_RELEASE_MS:OFF_RELEASE_MS + 2], "little"),
            "threshold": blob[OFF_THRESHOLD],
        }
        linkgroup = blob[OFF_LINKGROUP]
        # Name field: filter to printable ASCII; if no printable bytes
        # remain, treat as empty.  Real channel names (from Windows app)
        # are 7-bit ASCII like "TWEETER".  Non-ASCII bytes here usually
        # mean the offset is wrong for this firmware variant or the
        # field is uninitialized.
        name_raw = bytes(blob[OFF_NAME:OFF_NAME + NAME_LEN])
        name_clean = bytes(b for b in name_raw if 0x20 <= b < 0x7F)
        name = name_clean.rstrip().decode("ascii", errors="replace")

        db = (raw_vol - CHANNEL_VOL_OFFSET) / 10.0
        muted = (en_bit == 0)

        return {
            # legacy keys (preserved for backward compat)
            "db": db,
            "muted": muted,
            "delay": raw_delay,
            "subidx": spk_type,
            # new keys
            "polar": bool(polar),
            "byte_252": byte_252,  # semantic unknown — see protocol.OFF_BYTE_252
            "spk_type": spk_type,
            "hpf": hpf,
            "lpf": lpf,
            "mixer": mixer,
            "compressor": compressor,
            "linkgroup": linkgroup,
            "name": name,
            # raw blob retained so callers can re-parse fields we haven't
            # decoded yet (PEQ bands, ChannelLink etc.) without another
            # round-trip to the device.
            "raw": bytes(blob),
        }

    def get_channel(self, channel: int) -> dict:
        """Read per-channel state from the device and return it.

        Issues a ``cmd=0x77NN`` read, parses the 296-byte blob into the
        full channel-state dict (see :meth:`parse_channel_state_blob` for
        the field list), and seeds the in-memory cache with the subset
        :meth:`set_channel` needs to round-trip writes correctly.

        Returns the full parsed dict on success — keys include ``db``,
        ``muted``, ``delay``, ``subidx``, ``polar``, ``byte_252``,
        ``spk_type``, ``hpf``, ``lpf``, ``mixer``, ``compressor``,
        ``linkgroup``, ``name``, and ``raw`` (the original bytes).

        If the blob cannot be parsed (blob too short, or en_bit/vol out of
        range), returns the cached defaults and logs a warning.
        """
        self._channel_cache_init()
        blob = self.read_channel_state(channel)
        result = self.parse_channel_state_blob(blob, channel)
        if result is None:
            import logging
            logging.getLogger("dsp408.device").warning(
                "get_channel(%d): could not parse 296-byte blob "
                "(len=%d, blob[246:254]=%s). First 16 bytes: %s",
                channel,
                len(blob) if blob else 0,
                blob[246:254].hex() if len(blob) >= 254 else "(short)",
                blob[:16].hex() if blob else "(empty)",
            )
            return dict(self._channel_cache[channel])

        # Store discovered subidx + polar so set_channel() writes back the
        # correct DSP type and preserves the user's phase setting (rather than
        # silently flipping it back to 0).
        self._channel_cache[channel] = {
            "db": result["db"],
            "muted": result["muted"],
            "polar": bool(result.get("polar", False)),
            "delay": result["delay"],
            "subidx": result["subidx"],
        }
        return result

    def write_channel_param(
        self,
        channel: int,
        value: int,
        sub_index: int,
    ) -> None:
        """Low-level channel-write escape hatch (cmd=0x1F00..0x1F07).

        Payload layout observed in windows-04c-stream-nostream-stream:
            01 00 | value_le_u32 | 00 | sub_index

        ``sub_index`` is the **speaker-role / channel-type byte** stored
        at blob[253] (see :data:`protocol.CHANNEL_SUBIDX` and
        :data:`protocol.SPK_TYPE_NAMES`) — not a parameter selector as
        early reverse-engineering had hypothesized.  Prefer the typed
        wrappers (:meth:`set_channel`, :meth:`set_eq_band`,
        :meth:`set_crossover`) for normal use; this method exists only
        for replaying captured frames or probing unknown speaker-role
        values.
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("value must fit in u32")
        # 0x1f00..0x1f07 — channel index in the LOW byte. (Historically
        # `CMD_WRITE_CHANNEL_BASE | (channel << 8)` was used; that's a
        # bit-collision and gave 0x1f00 for every channel — fixed.)
        cmd = CMD_WRITE_CHANNEL_BASE + channel
        payload = (
            b"\x01\x00"
            + value.to_bytes(4, "little")
            + b"\x00"
            + bytes([sub_index & 0xFF])
        )
        self.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)

    # ── master volume + mute ───────────────────────────────────────────
    def get_master(self) -> tuple[float, bool]:
        """Read master volume + mute state.

        Returns:
            (db, muted) where db is in -60..+6 (1 dB resolution) and
            muted is True when the master mute bit is set.

        Decode of the 8-byte payload `[lvl, 00, 00, 32, 00, 32, mute, 00]`:
          dB = lvl - 60   (raw 60 = 0 dB, raw 0 = -60 dB, raw 66 = +6 dB)
          mute_bit byte[6]: 1 = unmuted/audible, 0 = muted
        """
        reply = self.read_raw(cmd=CMD_MASTER, category=CAT_STATE)
        p = bytes(reply.payload)
        if len(p) < 8:
            raise ProtocolError(f"master read returned {len(p)} bytes, want 8")
        lvl = p[0]
        muted = p[6] == 0
        return float(lvl - MASTER_LEVEL_OFFSET), muted

    def set_master(self, db: float, muted: bool) -> None:
        """Write both master level + mute in one frame.

        `db` is clamped to [-60, +6]. `muted` True flips the audible
        bit off (byte[6] = 0).
        """
        lvl = max(MASTER_LEVEL_MIN, min(MASTER_LEVEL_MAX,
                                        round(db + MASTER_LEVEL_OFFSET)))
        mute_bit = 0 if muted else 1
        payload = bytes([lvl, 0, 0, 0x32, 0, 0x32, mute_bit, 0])
        self.write_raw(cmd=CMD_MASTER, data=payload, category=CAT_STATE)

    def set_master_volume(self, db: float) -> None:
        """Set master volume in dB (-60..+6), preserving mute state."""
        _, muted = self.get_master()
        self.set_master(db, muted)

    def set_master_mute(self, muted: bool) -> None:
        """Set master mute on/off, preserving volume level."""
        db, _ = self.get_master()
        self.set_master(db, muted)

    # ── per-channel volume + mute ──────────────────────────────────────
    @staticmethod
    def _channel_payload(channel: int, db: float, muted: bool,
                         delay_samples: int = 0,
                         sub_index: int | None = None,
                         polar: bool = False) -> bytes:
        """Build the 8-byte per-channel write payload (cmd=0x1FNN).

        Layout (verified live on hardware, matches blob[246..253] read-back):

        .. code-block:: text

            [0] mute      0=muted, 1=audible (en_bit, INVERTED from leon)
            [1] polar     0=normal, 1=phase-inverted (180°)
            [2..3] gain_le16  raw = (dB×10)+600
            [4..5] delay_le16 samples
            [6] byte_6    semantic unknown — stored at blob[252] but does
                          NOT bypass EQ (was hypothesized to be `eq_mode`,
                          disproved live; see protocol.OFF_BYTE_252).
                          We always write 0 here.
            [7] subidx    DSP channel-type / speaker-role

        Args:
            polar: 180° phase invert. Empirically validated via Scarlett
                loopback on real hardware (Δphase = ±180° with 6° jitter).
            sub_index: DSP channel-type byte (blob[253]).  Pass the value
                previously returned by ``get_channel()["subidx"]`` to
                preserve the firmware's type assignment.  If None, falls
                back to ``CHANNEL_SUBIDX[channel]`` (factory defaults).
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        if not 0 <= delay_samples <= 0xFFFF:
            raise ValueError(f"delay_samples must fit in u16, got {delay_samples}")
        vol = max(CHANNEL_VOL_MIN, min(CHANNEL_VOL_MAX,
                                       round(db * 10 + CHANNEL_VOL_OFFSET)))
        en_bit = 0 if muted else 1
        pol_bit = 1 if polar else 0
        si = sub_index if sub_index is not None else CHANNEL_SUBIDX[channel]
        return bytes([
            en_bit, pol_bit,
            vol & 0xFF, (vol >> 8) & 0xFF,
            delay_samples & 0xFF, (delay_samples >> 8) & 0xFF,
            0, si,
        ])

    # In-memory cache of per-channel state we've written (and read back).
    # Channel reads via cmd=0x1f0X cat=0x04 return the EQ filter table
    # (296 bytes), not the volume header — so to support "set just the mute"
    # or "set just the volume" without losing the other field, we track what
    # we set.  Defaults (db=0, muted=False, delay=0, subidx=table default)
    # match the device's power-up state.
    #
    # The `subidx` field is populated by get_channel() from the live device
    # read (blob[253]).  Using the actual discovered value rather than the
    # CHANNEL_SUBIDX table default ensures that set_channel() preserves the
    # firmware's DSP type assignment even when a device is configured with a
    # non-default type (e.g. ch1 with subidx=0x12 on some devices).
    def _channel_cache_init(self) -> None:
        if not hasattr(self, "_channel_cache"):
            self._channel_cache: list[dict] = [
                {
                    "db": 0.0,
                    "muted": False,
                    "polar": False,
                    "delay": 0,
                    "subidx": CHANNEL_SUBIDX[ch],  # updated by get_channel()
                }
                for ch in range(8)
            ]

    def set_channel(self, channel: int, db: float, muted: bool,
                    delay_samples: int = 0,
                    polar: bool | None = None) -> None:
        """Write per-channel volume + mute + delay (+ optional polar) in one frame.

        Uses the subidx (DSP channel-type) from the in-memory cache.  If
        ``get_channel()`` has been called before, the cache holds the actual
        device type (possibly non-default); otherwise falls back to
        ``CHANNEL_SUBIDX[channel]``.  Preserving the correct subidx prevents
        accidentally overwriting the firmware's DSP type assignment.

        ⚠ **Startup write-drop quirk** (verified live 2026-04-19):
            The firmware silently drops the first ~5–6 cmd=0x1FNN writes
            that arrive faster than it can process — back-to-back writes
            with no intervening reads/sleeps lose their early entries even
            though every write returns a clean ACK.  Master writes and
            non-channel commands do NOT count toward this quota.

            Mitigation pattern (used by every loopback test): warm up by
            doing 8 set+read cycles before relying on writes to land::

                for ch in range(8):
                    dsp.set_channel(ch, db=0.0, muted=False)
                # then any audio measurement / time.sleep(>~1s) / per-ch
                # readback gives the firmware time to drain its queue.

            What does NOT work: 8 back-to-back set_channel() calls with
            no reads or sleeps in between — the firmware processes the
            queue in bulk and drops everything past its first slot.

        Args:
            polar: True/False to set/clear 180° phase invert; None (default)
                preserves the cached polar value so callers that don't care
                about polar don't accidentally flip it.
        """
        self._channel_cache_init()
        # Use the discovered subidx (from get_channel readback), falling back
        # to the CHANNEL_SUBIDX table default for channels not yet read or
        # for channels whose subidx is 0x00 (uninitialized firmware struct).
        cached_si = self._channel_cache[channel].get("subidx", 0)
        si = cached_si if cached_si != 0 else CHANNEL_SUBIDX[channel]
        # Default polar to cached value (preserve unless explicitly changed).
        eff_polar = (self._channel_cache[channel].get("polar", False)
                     if polar is None else bool(polar))
        payload = self._channel_payload(channel, db, muted, delay_samples,
                                        sub_index=si, polar=eff_polar)
        cmd = CMD_WRITE_CHANNEL_BASE + channel  # 0x1f00..0x1f07
        self.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)
        # Update cache (preserve subidx + new polar for next set call).
        self._channel_cache[channel] = {
            "db": float(db),
            "muted": bool(muted),
            "polar": eff_polar,
            "delay": int(delay_samples),
            "subidx": si,
        }

    def set_channel_polar(self, channel: int, polar: bool) -> None:
        """Toggle 180° phase invert on the channel, preserving volume/mute/delay."""
        self._channel_cache_init()
        c = self._channel_cache[channel]
        self.set_channel(channel, c["db"], c["muted"], c["delay"], polar=polar)

    def set_channel_volume(self, channel: int, db: float) -> None:
        """Set per-channel volume in dB (-60..0), preserving mute + delay."""
        self._channel_cache_init()
        c = self._channel_cache[channel]
        self.set_channel(channel, db, c["muted"], c["delay"])

    def set_channel_mute(self, channel: int, muted: bool) -> None:
        """Set per-channel mute on/off, preserving volume + delay."""
        self._channel_cache_init()
        c = self._channel_cache[channel]
        self.set_channel(channel, c["db"], muted, c["delay"])

    def get_channel_cached(self, channel: int) -> dict:
        """Return last-written (db, muted, delay) for a channel.

        The device doesn't expose a clean per-channel volume read (the
        cmd=0x1f0X cat=0x04 read returns the channel's EQ filter table
        instead of the volume header). So we mirror what we wrote.
        """
        self._channel_cache_init()
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        return dict(self._channel_cache[channel])

    # ── routing matrix ─────────────────────────────────────────────────
    def set_routing_levels(self, output_idx: int,
                           levels: list[int] | tuple[int, ...]) -> None:
        """Set per-input mix levels for one output channel.

        Each routing cell is a u8 linear-amplitude scalar (verified live
        on real hardware via Scarlett loopback test in
        ``tests/loopback/test_routing_percentage.py``):

          * 0   → off (silent)
          * 100 → unity gain (the value our boolean ``set_routing()`` writes)
          * 50  → -6 dB
          * 25  → -12 dB
          * 200 → +6 dB  (firmware allows BOOST above unity!)
          * 255 → +8.1 dB (max u8 = max headroom, undocumented)

        The Windows GUI never uses values other than 0/100, but the
        firmware accepts the full 0..255 range with a precise
        ``20·log10(level/100)`` dB curve.

        Args:
            output_idx: 0..7 (corresponds to OUT 1..OUT 8)
            levels: 4 ints in [0, 255], one per IN1..IN4

        Raises:
            ValueError: output_idx out of range, or any level out of [0, 255].
        """
        if not 0 <= output_idx <= 7:
            raise ValueError(f"output_idx must be in 0..7, got {output_idx}")
        if len(levels) != 4:
            raise ValueError(f"levels must have 4 entries, got {len(levels)}")
        for i, lvl in enumerate(levels):
            if not 0 <= lvl <= 255:
                raise ValueError(
                    f"levels[{i}]={lvl} out of u8 range [0, 255]")
        cmd = CMD_ROUTING_BASE + output_idx  # 0x2100..0x2107
        # Bytes 4..7 are reserved/zero in the wire format.
        payload = bytes(list(levels) + [0, 0, 0, 0])
        self.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)

    # ── factory reset (magic-word write to cmd=0x2000) ─────────────────
    # Decoded 2026-04-19 from captures/reset_to_defaults.pcapng on the
    # reverse-engineering branch. The official GUI's "Reset to Defaults"
    # action emits exactly four writes after the connect handshake:
    #
    #   1. cmd=0x00  (preset_name) ← "Custom"
    #   2. cmd=0x2000 (write_global) ← `06 1f 00 00 20 4e 00 01`  ← THE MAGIC
    #   3. cmd=0x00  (preset_name) ← "Custom"
    #   4. cmd=0x00  (preset_name) ← "Custom"
    #
    # The 8-byte magic looks structurally like:
    #     [0..1]  06 1f      — register selector 0x1F06 LE (= 0x061F BE = 1567,
    #                          matching the leon decompile's "register 1567"
    #                          claim for factory reset)
    #     [2..3]  00 00      — pad / alignment
    #     [4..7]  20 4e 00 01 — magic value (0x01004E20 LE = 16,797,728)
    #
    # We don't need to understand the field structure to drive it: send
    # the captured 8 bytes verbatim. Live-verified on the rig 2026-04-19
    # with full state diff (master, per-channel volume/mute/delay/polar,
    # routing matrix, EQ bands all returned to factory defaults).
    FACTORY_RESET_PAYLOAD = bytes.fromhex("061f0000204e0001")

    def factory_reset(self) -> None:
        """Replay the GUI's "Reset to Defaults" 4-write sequence.

        Wire encoding is **verified** (matches the captured GUI bytes
        exactly) but **behavior is partially unverified**.  Sequence:
          1. preset name → "Custom"            (cmd=0x00, cat=0x09)
          2. magic-word write                  (cmd=0x2000, cat=0x04,
             payload = ``06 1f 00 00 20 4e 00 01``)
          3. preset name → "Custom"            (×2, mimics GUI behavior)

        What we observed live (2026-04-19):
          * The magic frame is accepted with a ~430 ms ack delay (vs.
            ~10 ms for normal writes) — the firmware is doing real work.
          * The preset name does change to "Custom" reliably.
          * **In our smoke test the per-channel state (volume, mute,
            delay, polar, routing, EQ, compressor) did NOT visibly revert
            via** ``read_channel_state()`` **right after the magic.**

        That mismatch with the action's name ("Reset to Defaults") is
        unresolved.  Hypotheses: it may only persist to flash and take
        effect on the next power cycle; it may reset a subsystem we
        don't currently read back; or the GUI capture happened to be
        on an already-defaulted device so we can't tell what would have
        changed.  We need either a "modify-then-reset" capture or a
        physical power-cycle test to pin it down.

        Until then, treat this as "send the canonical bytes the GUI
        sends" — useful for round-tripping, possibly NOT useful as an
        actual factory reset.  See
        ``captures-needed-from-windows.md`` item #2 on the
        reverse-engineering branch.
        """
        # Step 1: name to "Custom"
        self.write_raw(cmd=CMD_PRESET_NAME,
                       data=b"Custom\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
                       category=CAT_STATE)
        # Step 2: the magic write — this is what actually triggers the reset.
        # Devices take ~430 ms to ack this frame (vs. ~10 ms for normal writes)
        # because the firmware is wiping the entire parameter block. Category
        # MUST be CAT_PARAM (0x04) — the GUI uses CAT_STATE for preset-name
        # writes but CAT_PARAM for the magic; sending the magic with
        # CAT_STATE is a silent no-op (verified live 2026-04-19).
        self.write_raw(cmd=CMD_WRITE_GLOBAL,
                       data=self.FACTORY_RESET_PAYLOAD,
                       category=CAT_PARAM,
                       timeout_ms=3000)
        # Step 3+4: name to "Custom" again, twice (matches GUI exactly)
        for _ in range(2):
            self.write_raw(cmd=CMD_PRESET_NAME,
                           data=b"Custom\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
                           category=CAT_STATE)
        # Invalidate the per-channel cache — every channel's state has
        # just been wiped back to factory defaults.
        if hasattr(self, "_channel_cache"):
            for ch in range(8):
                self._channel_cache[ch] = {
                    "db": 0.0,
                    "muted": False,
                    "polar": False,
                    "delay": 0,
                    "subidx": CHANNEL_SUBIDX[ch],
                }

    # ── load factory preset (still UNVERIFIED) ─────────────────────────
    # The Windows GUI's preset-load action has NOT been captured yet.
    # leon's decompile suggests `0xB500 | preset_id` to register 1567 but
    # we never validated that on the wire. Stub kept so the MQTT button
    # has a target; do NOT rely on it.
    def load_factory_preset(self, preset_id: int) -> None:
        """⚠ KNOWN-BROKEN: intended to load one of the 6 built-in presets.

        Wire encoding is unverified.  Need a fresh capture (see
        captures-needed-from-windows.md item #3 on the reverse-engineering
        branch).
        """
        if not 1 <= preset_id <= 6:
            raise ValueError(f"preset_id must be 1..6, got {preset_id}")
        # Best guess from the leon decompile — likely wrong; do not rely on it.
        magic = 0xB500 | preset_id
        self.write_raw(cmd=0x061F,
                       data=bytes([magic & 0xFF, (magic >> 8) & 0xFF]),
                       category=CAT_STATE)

    def set_routing(self, output_idx: int,
                    in1: bool, in2: bool, in3: bool, in4: bool) -> None:
        """Set which inputs feed a given output (boolean convenience wrapper).

        Calls :meth:`set_routing_levels` with each True bool mapped to the
        full-scale ``ROUTING_ON`` (= 100) and each False to ``ROUTING_OFF``
        (= 0).  For partial / boosted levels use ``set_routing_levels``
        directly.
        """
        levels = [
            ROUTING_ON if in1 else ROUTING_OFF,
            ROUTING_ON if in2 else ROUTING_OFF,
            ROUTING_ON if in3 else ROUTING_OFF,
            ROUTING_ON if in4 else ROUTING_OFF,
        ]
        self.set_routing_levels(output_idx, levels)

    # ── crossover (HPF + LPF per channel) ──────────────────────────────
    # Filter type values for blob[256] (HPF) and blob[260] (LPF).  The
    # Windows GUI dropdown labels them "Butterworth / Bessel / Linkwitz-
    # Riley / Defeat".  Empirically validated 2026-04-19 via Scarlett
    # loopback + discrete-tone sweep — see
    # tests/loopback/test_crossover_characterization.py and the saved
    # response plot at docs/measurements/crossover_filter_types.png.
    #
    # Surprise finding: type=3 ("Defeat" in the UI) produces the IDENTICAL
    # filter response as type=2 (Linkwitz-Riley) — same -3 dB knee, same
    # -6 dB knee, same asymptotic slope, all within 0.3 dB measurement
    # noise.  The Windows GUI exposes it as a separate option but the
    # firmware aliases it to LR.
    HPF_LPF_FILTER_BUTTERWORTH = 0
    HPF_LPF_FILTER_BESSEL = 1
    HPF_LPF_FILTER_LR = 2          # Linkwitz-Riley
    HPF_LPF_FILTER_DEFEAT = 3      # Windows-UI label; aliases to LR in firmware

    # Slope is dB/octave: 0..7 = 6/12/18/24/30/36/42/48 dB/oct.  Value 8
    # bypasses the filter — the channel passes audio through with flat
    # magnitude regardless of the freq parameter (verified live via
    # discrete-tone sweep on the loopback rig: HPF slope=8 + LPF slope=8
    # gives +2 dB ±0.1 across 50 Hz–10 kHz, identical to an explicitly
    # wide-open Butterworth).  Hardware default = 1 (12 dB/oct).
    HPF_LPF_SLOPE_OFF = 8

    def set_crossover(
        self,
        channel: int,
        hpf_freq: int,
        hpf_filter: int,
        hpf_slope: int,
        lpf_freq: int,
        lpf_filter: int,
        lpf_slope: int,
    ) -> None:
        """Write the per-channel HPF + LPF crossover record in one frame.

        Encoding decoded from ``captures/full-sequence.pcapng`` (Windows
        DSP-408 V1.24 GUI changing filter types) and verified live on
        real hardware 2026-04-19 — the 8-byte payload mirrors
        ``blob[254..261]`` exactly, so a write here shows up surgically
        at those offsets in the next ``read_channel_state()`` blob.

        Args:
            channel:    0..7 (output channel index).
            hpf_freq:   high-pass cutoff in Hz, u16 (firmware default 20).
            hpf_filter: 0=Butterworth, 1=Bessel, 2=Linkwitz-Riley,
                        3=Defeat (Windows-UI label; aliases LR — see the
                        ``HPF_LPF_FILTER_*`` class constants).
            hpf_slope:  dB/octave step: 0=6, 1=12, 2=18, 3=24, 4=30,
                        5=36, 6=42, 7=48; 8 bypasses the filter
                        entirely (audio passes through flat regardless
                        of ``hpf_freq``).
            lpf_freq:   low-pass cutoff in Hz (firmware default 20000).
            lpf_filter: same range as ``hpf_filter``.
            lpf_slope:  same range as ``hpf_slope``.

        Raises:
            ValueError: any param out of range.
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        for name, val in (("hpf_freq", hpf_freq), ("lpf_freq", lpf_freq)):
            if not 0 <= val <= 0xFFFF:
                raise ValueError(f"{name} must fit in u16, got {val}")
        for name, val in (("hpf_filter", hpf_filter),
                          ("lpf_filter", lpf_filter)):
            if not 0 <= val <= 3:
                raise ValueError(f"{name} must be 0..3, got {val}")
        for name, val in (("hpf_slope", hpf_slope), ("lpf_slope", lpf_slope)):
            if not 0 <= val <= 8:
                raise ValueError(f"{name} must be 0..8, got {val}")
        payload = bytes([
            hpf_freq & 0xFF, (hpf_freq >> 8) & 0xFF,
            hpf_filter, hpf_slope,
            lpf_freq & 0xFF, (lpf_freq >> 8) & 0xFF,
            lpf_filter, lpf_slope,
        ])
        cmd = CMD_WRITE_CROSSOVER_BASE + channel  # 0x12000..0x12007
        self.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)

    # ── parametric EQ (10 bands per channel) ────────────────────────────
    # Live-validated 2026-04-19 via Scarlett loopback + pink-noise PSD
    # ratio (see tests/loopback/_probe_eq_pink.py for the calibration
    # script).  Each output channel has 10 peaking-EQ bands at default
    # ISO octave centers ``EQ_DEFAULT_FREQS_HZ`` (31, 65, 125, 250, 500,
    # 1000, 2000, 4000, 8000, 16000 Hz).  Each band has independent
    # freq / gain / Q.
    #
    # Band-count limit (verified by _probe_eq_extra_bands.py):
    #   * Bands 10..30 are silently ACKed by the firmware but produce
    #     **no** acoustic effect — the Windows GUI's "10 bands" is the
    #     real upper bound, not a UI limit.  Writes to higher band indices
    #     don't fail but don't do anything either.
    #
    # Frequency / band-index independence (verified same probe):
    #   * The 10 default centres are *defaults*, not constraints.  You
    #     can put band 0 at 16 kHz and band 9 at 31 Hz and both peaks
    #     appear at the requested fcs.  Bands are independent slots.
    #
    # The bandwidth byte is encoded as a fixed-point reciprocal of Q:
    #
    #     bandwidth_byte ≈ EQ_Q_BW_CONSTANT (= 256) / Q
    #
    # Measured peak BW₃ ↔ b4 across the validated b4∈[26..208] range:
    #   b4= 26 → BW₃≈129 Hz (Q≈7.8) ;  b4= 52 → BW₃≈223 Hz (Q≈4.5, default)
    #   b4=104 → BW₃≈410 Hz (Q≈2.5) ;  b4=208 → BW₃≈873 Hz (Q≈1.2)
    # All peak gains land within ±0.1 dB of the requested value.
    # Re-export the protocol constants on the class surface so callers
    # can write `Device.EQ_BAND_COUNT` without importing protocol.
    # Local aliases avoid the self-assignment warning some linters emit
    # when class-body names shadow module-level imports of the same name.
    EQ_BAND_COUNT = int(EQ_BAND_COUNT)
    EQ_Q_BW_CONSTANT = float(EQ_Q_BW_CONSTANT)
    EQ_DEFAULT_FREQS_HZ = tuple(EQ_DEFAULT_FREQS_HZ)

    @staticmethod
    def q_to_bandwidth_byte(q: float) -> int:
        """Convert a desired Q to the firmware's bandwidth byte.

        Returns an int clamped to 1..255 (the byte is unsigned and 0
        would be a divide-by-zero in the reciprocal encoding).
        """
        if q <= 0:
            raise ValueError(f"q must be positive, got {q}")
        b4 = round(EQ_Q_BW_CONSTANT / q)
        return max(1, min(255, b4))

    @staticmethod
    def bandwidth_byte_to_q(b4: int) -> float:
        """Inverse of :meth:`q_to_bandwidth_byte`."""
        if not 1 <= b4 <= 255:
            raise ValueError(f"b4 must be 1..255, got {b4}")
        return EQ_Q_BW_CONSTANT / b4

    def set_eq_band(
        self,
        channel: int,
        band: int,
        freq_hz: int,
        gain_db: float,
        q: float | None = None,
        *,
        bandwidth_byte: int | None = None,
    ) -> None:
        """Write one parametric-EQ band on one output channel.

        The encoding maps the GUI's "freq / gain / Q" controls onto an
        8-byte payload mirrored at blob[band*8 .. band*8+8] in the
        296-byte channel state struct (verified live: a write here shows
        up surgically at those offsets in the next read_channel_state).

        Args:
            channel:  0..7 (output channel index).
            band:     0..9 (band index; default centers in
                      ``EQ_DEFAULT_FREQS_HZ``).
            freq_hz:  band centre frequency in Hz, u16.
            gain_db:  ±60 dB (clamped); raw = (dB×10) + 600.
            q:        peaking-EQ quality factor.  Higher = narrower peak.
                      The firmware default is ~5 (b4=0x34).  Mutually
                      exclusive with ``bandwidth_byte``; if both are
                      omitted the firmware default of b4=0x34 is used.
            bandwidth_byte: raw byte [4] of the payload (1..255).
                      Use this only if you need to write an explicit byte
                      value (e.g. for replaying a captured frame).  Use
                      ``q`` for normal API use.

        Raises:
            ValueError: any param out of range, or both q + bandwidth_byte
                given.
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        if not 0 <= band < EQ_BAND_COUNT:
            raise ValueError(
                f"band must be in 0..{EQ_BAND_COUNT - 1}, got {band}")
        if not 0 <= freq_hz <= 0xFFFF:
            raise ValueError(f"freq_hz must fit in u16, got {freq_hz}")
        if q is not None and bandwidth_byte is not None:
            raise ValueError("pass q OR bandwidth_byte, not both")
        if bandwidth_byte is None:
            b4 = self.q_to_bandwidth_byte(q) if q is not None else 0x34
        else:
            if not 1 <= bandwidth_byte <= 255:
                raise ValueError(
                    f"bandwidth_byte must be 1..255, got {bandwidth_byte}")
            b4 = bandwidth_byte
        raw = max(EQ_GAIN_RAW_MIN, min(EQ_GAIN_RAW_MAX,
                                       round(gain_db * 10 + CHANNEL_VOL_OFFSET)))
        payload = bytes([
            freq_hz & 0xFF, (freq_hz >> 8) & 0xFF,
            raw & 0xFF, (raw >> 8) & 0xFF,
            b4, 0, 0, 0,
        ])
        cmd = CMD_WRITE_EQ_BAND_BASE + (band << 8) + channel
        self.write_raw(cmd=cmd, data=payload, category=CAT_PARAM)

    # ── per-channel compressor ─────────────────────────────────────────
    def set_compressor(
        self,
        channel: int,
        attack_ms: int,
        release_ms: int,
        threshold: int,
        *,
        all_pass_q: int = 420,
        enable: bool = True,
    ) -> None:
        """Write per-channel compressor parameters (cmd=0x2300+ch).

        Wire encoding decoded from
        ``captures/windows-04b-volumes-mute-presets.pcapng`` and verified
        live 2026-04-19: the 8-byte payload lands at blob[278..285] (NOT
        270..277 as one earlier note suggested — those bytes are a
        read-only shadow that ignores writes).  All four distinctive
        values (attack/release/threshold/all_pass_q) round-trip exactly
        through ``read_channel_state()`` after this write.

        ⚠ **Compressor *behavior* still unverified.** The wire format is
        confirmed but we have NOT yet driven the loopback rig hard
        enough to fit an attack/release/threshold curve on a real audio
        signal — so the *units* of ``threshold`` (claimed dB-scaled) and
        the meaning of byte[7] ``enable`` are still inferred, not
        measured.  Use with caution until the compressor probe lands.

        Args:
            channel:     0..7 (output index).
            attack_ms:   compressor attack time in ms (u16; firmware default 56).
            release_ms:  release time in ms (u16; firmware default 500).
            threshold:   level above which compression engages (u8; units
                         not yet calibrated).
            all_pass_q:  internal all-pass-filter Q used for sidechain
                         shaping (u16; firmware default 420). Leave
                         unchanged unless you know what you're doing.
            enable:      payload byte[7]: True=on, False=bypass (guess
                         from a single capture; behavior not verified).
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"channel must be in 0..7, got {channel}")
        if not 0 <= attack_ms <= 0xFFFF:
            raise ValueError(f"attack_ms must fit u16, got {attack_ms}")
        if not 0 <= release_ms <= 0xFFFF:
            raise ValueError(f"release_ms must fit u16, got {release_ms}")
        if not 0 <= threshold <= 0xFF:
            raise ValueError(f"threshold must fit u8, got {threshold}")
        if not 0 <= all_pass_q <= 0xFFFF:
            raise ValueError(f"all_pass_q must fit u16, got {all_pass_q}")
        payload = bytes([
            all_pass_q & 0xFF, (all_pass_q >> 8) & 0xFF,
            attack_ms & 0xFF, (attack_ms >> 8) & 0xFF,
            release_ms & 0xFF, (release_ms >> 8) & 0xFF,
            threshold & 0xFF,
            1 if enable else 0,
        ])
        cmd = CMD_WRITE_COMPRESSOR_BASE + channel
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
