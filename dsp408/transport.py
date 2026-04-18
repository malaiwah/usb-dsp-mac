"""dsp408.transport — HID transport with hidapi API compatibility shim.

Handles:
  * Two different hidapi Python APIs on Linux / macOS:
      - Debian python3-hid ships legacy `hid.device()` (positional args)
      - PyPI hidapi exposes both `hid.device` (legacy) and `hid.Device`
  * Single-frame and multi-frame response reassembly.
  * Report-ID prefix byte (0x00) required on Linux/macOS for interrupt OUT.
"""
from __future__ import annotations

import time

from .protocol import (
    FRAME_SIZE,
    Frame,
    parse_frame,
)

DEFAULT_TIMEOUT_MS = 1000


class HidCompat:
    """Thin wrapper over whichever hidapi flavor is available.

    Exposes: open_vid_pid(vid, pid) -> self, open_path(path) -> self,
             write(bytes), read(nbytes, timeout_ms) -> bytes|None, close().
    """

    def __init__(self):
        import hid  # deferred so library import doesn't fail without hidapi

        self._hid = hid
        self._dev = None
        # Which flavor do we have?
        self._legacy = not hasattr(hid, "Device")

    # ----- enumeration -----
    @staticmethod
    def enumerate(vid: int, pid: int) -> list[dict]:
        import hid

        return hid.enumerate(vid, pid)

    # ----- opening -----
    def open_vid_pid(self, vid: int, pid: int) -> HidCompat:
        if self._legacy:
            d = self._hid.device()
            d.open(vid, pid)
            d.set_nonblocking(0)
            self._dev = d
        else:
            self._dev = self._hid.Device(vid, pid)
        return self

    def open_path(self, path: bytes) -> HidCompat:
        if self._legacy:
            d = self._hid.device()
            d.open_path(path)
            d.set_nonblocking(0)
            self._dev = d
        else:
            self._dev = self._hid.Device(path=path)
        return self

    # ----- I/O -----
    def write(self, data: bytes) -> int:
        """Write a HID report. `data` MUST already include the leading
        report-ID byte (0x00) for devices that use the default report."""
        if self._dev is None:
            raise RuntimeError("device not opened")
        return self._dev.write(data)

    def read(self, nbytes: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bytes:
        if self._dev is None:
            raise RuntimeError("device not opened")
        if self._legacy:
            # Legacy cython-hidapi: positional args only, timeout in ms
            raw = self._dev.read(nbytes, timeout_ms)
        else:
            # Newer hid.Device: keyword arg `timeout`
            try:
                raw = self._dev.read(nbytes, timeout=timeout_ms)
            except TypeError:
                raw = self._dev.read(nbytes, timeout_ms)
        return bytes(raw) if raw else b""

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    # ----- context -----
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Transport:
    """Frame-level transport on top of an HidCompat connection.

    Responsibilities:
      * Prepend the 0x00 report-ID byte on every write.
      * Drain zero-length "placeholder" reads that Linux usbmon sometimes
        emits between real 64-byte packets.
      * Reassemble multi-frame responses (declared payload > 48 bytes).
    """

    def __init__(self, hid_conn: HidCompat):
        self.hid = hid_conn

    # ----- raw send -----
    def send_frame(self, frame64: bytes) -> None:
        if len(frame64) != FRAME_SIZE:
            raise ValueError(f"expected {FRAME_SIZE} bytes, got {len(frame64)}")
        self.hid.write(b"\x00" + frame64)

    # ----- single frame recv -----
    def read_frame(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Frame | None:
        """Read one DSP-408 frame, skipping empty/zero-length reads."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                break
            raw = self.hid.read(FRAME_SIZE, min(remaining, 200))
            if not raw:
                continue
            frm = parse_frame(raw)
            if frm is not None:
                return frm
            # Non-DSP408 frame — ignore and keep draining
        return None

    # ----- multi-frame recv -----
    def read_response(
        self, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> Frame | None:
        """Read a complete response, reassembling continuation frames for
        multi-frame payloads (e.g. cmd=0x77NN returns 296 bytes).

        The returned Frame has its .payload filled with ALL declared bytes,
        while .raw is just the first HID frame. `timeout_ms` is a global
        budget — continuation reads share the remaining time rather than
        getting a fresh `timeout_ms` each.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        first = self.read_frame(timeout_ms=timeout_ms)
        if first is None:
            return None
        if not first.is_multi_frame():
            return first
        collected = bytearray(first.payload)
        want = first.payload_len
        while len(collected) < want:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            raw = self.hid.read(FRAME_SIZE, min(remaining_ms, 500))
            if not raw:
                continue  # poll until deadline
            # Continuation frames are raw 64-byte blocks with no framing.
            collected.extend(raw[: min(len(raw), want - len(collected))])
        # Return a Frame with reassembled payload
        return Frame(
            direction=first.direction,
            seq=first.seq,
            category=first.category,
            cmd=first.cmd,
            payload_len=first.payload_len,
            payload=bytes(collected[: first.payload_len]),
            checksum=first.checksum,
            checksum_ok=first.checksum_ok,
            raw=first.raw,
        )

    # ----- drain -----
    def drain(self, timeout_ms: int = 50) -> int:
        """Discard any pending input frames. Returns count drained."""
        n = 0
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            raw = self.hid.read(FRAME_SIZE, 20)
            if not raw:
                break
            n += 1
        return n
