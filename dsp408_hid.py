#!/usr/bin/env python3
"""
DSP-408 USB HID interface — clean, correct implementation.

Frame format (inside the 64-byte HID report):
    10 02 00 01 [LEN] [PAYLOAD...] 10 03 [CHK]

    CHK = XOR(LEN, PAYLOAD[0], PAYLOAD[1], ...)

    Note: checksum covers ONLY the LEN byte and payload bytes,
    NOT the STX (10 02) or ETX (10 03) framing bytes.

Write on macOS with hidapi:
    dev.write(bytes([0x00]) + frame_64_bytes)   ← 65 bytes total
    hidapi strips the 0x00 report-ID and sends the 64-byte frame.

Write on Linux with /dev/hidraw*:
    os.write(fd, frame_64_bytes)                ← 64 bytes, no report-ID

Initialization sequence (required after open, before any DSP command):
    1. OP_INIT (0x10) → device starts responding
    2. OP_FIRMWARE (0x13) → optional, gets firmware string
    3. OP_DEVICE_INFO (0x2C) → checks lock status
    4. read config if needed (9 pages via 0x27)
    5. OP_ACTIVATE (0x12) → applies/confirms config

Confirmed from:
    - miniDSP-Linux protocol.py (same protocol, different device)
    - README_DSP408.md handshake example: TX 10 02 00 01 01 10 10 03 11
    - Firmware disassembly: fn_c114 dispatches on rx_buf[4] (0xA1/0xA2)
"""

from __future__ import annotations

import os
import select
import sys
import time
from typing import Optional

try:
    import hid
    _HIDAPI = True
except ImportError:
    _HIDAPI = False

VID = 0x0483
PID = 0x5750
REPORT_SIZE = 64  # HID report is exactly 64 bytes (no report-ID in descriptor)

# ── Opcodes ───────────────────────────────────────────────────────────────────
OP_INIT         = 0x10
OP_ACTIVATE     = 0x12
OP_FIRMWARE     = 0x13
OP_PRESET_INDEX = 0x14
OP_LOAD_PRESET  = 0x20
OP_STORE_PRESET = 0x21
OP_PRESET_HDR   = 0x22
OP_READ_CONFIG  = 0x27
OP_STORE_NAME   = 0x26
OP_READ_NAME    = 0x29
OP_DEVICE_INFO  = 0x2C
OP_LOPASS       = 0x31
OP_HIPASS       = 0x32
OP_PEQ          = 0x33
OP_GAIN         = 0x34
OP_MUTE         = 0x35
OP_PHASE        = 0x36
OP_DELAY        = 0x38
OP_MATRIX       = 0x3A
OP_LINK         = 0x3B
OP_PEQ_BYPASS   = 0x3C
OP_POLL         = 0x40

# Config read: device responds with OP_CONFIG_RESP
OP_CONFIG_RESP  = 0x24
CONFIG_PAGES    = 9
CONFIG_PAGE_SIZE = 50


# ── Frame encoding / decoding ─────────────────────────────────────────────────

def _checksum(length: int, payload: bytes) -> int:
    """XOR of length byte and all payload bytes."""
    chk = length
    for b in payload:
        chk ^= b
    return chk & 0xFF


def build_frame(payload: bytes) -> bytes:
    """Encode payload into a 64-byte HID OUT report.

    Frame layout:
        [0] 0x10  STX high
        [1] 0x02  STX low
        [2] 0x00  source (host)
        [3] 0x01  destination (device)
        [4] LEN   payload length
        [5..5+LEN-1] payload bytes
        [5+LEN]   0x10  ETX high
        [5+LEN+1] 0x03  ETX low
        [5+LEN+2] CHK   XOR(LEN, payload...)
        remaining: zero-padded to 64 bytes
    """
    n = len(payload)
    chk = _checksum(n, payload)
    frame = bytes([0x10, 0x02, 0x00, 0x01, n]) + payload + bytes([0x10, 0x03, chk])
    return frame.ljust(REPORT_SIZE, b'\x00')


def parse_frame(data: bytes) -> Optional[bytes]:
    """Parse a 64-byte HID IN report.

    Returns the payload bytes, or None if framing / checksum is invalid.
    The payload includes the opcode as the first byte.
    """
    if len(data) < 8:
        return None
    if data[0] != 0x10 or data[1] != 0x02:
        return None
    n = data[4]
    if 5 + n + 3 > len(data):
        return None
    payload = data[5:5 + n]
    if data[5 + n] != 0x10 or data[5 + n + 1] != 0x03:
        return None
    expected = _checksum(n, payload)
    if data[5 + n + 2] != expected:
        return None
    return bytes(payload)


# ── Command builders ──────────────────────────────────────────────────────────

def cmd_init()        -> bytes: return build_frame(bytes([OP_INIT]))
def cmd_activate()    -> bytes: return build_frame(bytes([OP_ACTIVATE]))
def cmd_firmware()    -> bytes: return build_frame(bytes([OP_FIRMWARE]))
def cmd_device_info() -> bytes: return build_frame(bytes([OP_DEVICE_INFO]))
def cmd_poll()        -> bytes: return build_frame(bytes([OP_POLL]))
def cmd_preset_hdr()  -> bytes: return build_frame(bytes([OP_PRESET_HDR]))
def cmd_preset_index()-> bytes: return build_frame(bytes([OP_PRESET_INDEX]))

def cmd_read_name(slot: int) -> bytes:
    """Read preset name by request index 0–29 (U01–U30)."""
    return build_frame(bytes([OP_READ_NAME, slot & 0xFF]))

def cmd_read_config(page: int) -> bytes:
    """Request config page 0–8.  Device replies with opcode 0x24."""
    return build_frame(bytes([OP_READ_CONFIG, page & 0xFF]))

def cmd_load_preset(slot: int) -> bytes:
    """Load preset: slot 0=F00, 1=U01 … 30=U30."""
    return build_frame(bytes([OP_LOAD_PRESET, slot & 0xFF]))

def cmd_store_preset_name(name: str) -> bytes:
    """Store preset name — send BEFORE cmd_store_preset()."""
    encoded = name[:14].encode('ascii', errors='replace').ljust(14, b' ')
    return build_frame(bytes([OP_STORE_NAME]) + encoded)

def cmd_store_preset(slot: int) -> bytes:
    """Write active settings to user preset slot 1–30 (never 0)."""
    if slot == 0:
        raise ValueError("slot 0 is the factory preset; never overwrite it")
    return build_frame(bytes([OP_STORE_PRESET, slot & 0xFF]))

def cmd_gain(channel: int, raw: int) -> bytes:
    """Set gain.  channel: 0–3 inputs, 4–11 outputs.  raw: 0–400."""
    raw = max(0, min(400, raw))
    return build_frame(bytes([OP_GAIN, channel & 0xFF, raw & 0xFF, (raw >> 8) & 0xFF]))

def cmd_mute(channel: int, muted: bool) -> bytes:
    """Mute/unmute channel (0–3 inputs, 4–11 outputs)."""
    return build_frame(bytes([OP_MUTE, channel & 0xFF, 0x01 if muted else 0x00]))

def cmd_phase(channel: int, inverted: bool) -> bytes:
    """Phase invert (0–3 inputs, 4–11 outputs)."""
    return build_frame(bytes([OP_PHASE, channel & 0xFF, 0x01 if inverted else 0x00]))

def cmd_delay(channel: int, samples: int) -> bytes:
    """Output delay.  channel: 4–11.  samples: 0–32640 (1 sample = 1/48000 s)."""
    samples = max(0, min(32640, samples))
    return build_frame(bytes([OP_DELAY, channel & 0xFF, samples & 0xFF, (samples >> 8) & 0xFF]))

def cmd_lopass(channel: int, freq_raw: int, slope: int = 0) -> bytes:
    """Low-pass crossover.  channel: 4–11.  freq_raw: 0–300.  slope: 0=bypass."""
    freq_raw = max(0, min(300, freq_raw))
    return build_frame(bytes([OP_LOPASS, channel & 0xFF, freq_raw & 0xFF, (freq_raw >> 8) & 0xFF, slope & 0xFF]))

def cmd_hipass(channel: int, freq_raw: int, slope: int = 0) -> bytes:
    """High-pass crossover.  channel: 4–11.  freq_raw: 0–300.  slope: 0=bypass."""
    freq_raw = max(0, min(300, freq_raw))
    return build_frame(bytes([OP_HIPASS, channel & 0xFF, freq_raw & 0xFF, (freq_raw >> 8) & 0xFF, slope & 0xFF]))

def cmd_peq(channel: int, band: int, gain_raw: int, freq_raw: int,
            q_raw: int, ftype: int = 0, bypass: bool = False) -> bytes:
    """PEQ band.  channel: 4–11 (outputs).  band: 0-indexed."""
    gain_raw = max(0, min(240, gain_raw))
    freq_raw = max(0, min(300, freq_raw))
    q_raw    = max(0, min(100, q_raw))
    return build_frame(bytes([
        OP_PEQ, channel & 0xFF, band & 0xFF,
        gain_raw & 0xFF, (gain_raw >> 8) & 0xFF,
        freq_raw & 0xFF, (freq_raw >> 8) & 0xFF,
        q_raw & 0xFF, ftype & 0xFF,
        0x01 if bypass else 0x00,
    ]))

def cmd_matrix(output_ch: int, input_mask: int) -> bytes:
    """Matrix routing.  output_ch: 4–11.  input_mask: bitmask In0=1, In1=2, In2=4, In3=8."""
    return build_frame(bytes([OP_MATRIX, output_ch & 0xFF, input_mask & 0x0F]))


# ── Response parsers ──────────────────────────────────────────────────────────

def parse_levels(payload: bytes) -> Optional[dict]:
    """Parse OP_POLL (0x40) response → input/output level dict.

    The 4x4 Mini sends 28 bytes; DSP-408 (4x8) may send more.
    Each channel is 3 bytes: [lo, hi, instant] → uint16 LE + instant sample.
    """
    if not payload or payload[0] != OP_POLL:
        return None
    # Extract as many 3-byte triplets as the payload contains after the opcode
    triplets = []
    for i in range(1, len(payload) - 2, 3):
        val = payload[i] + payload[i + 1] * 256
        triplets.append(val)
    return {'channels': triplets, 'raw': payload.hex()}


def parse_config_page(payload: bytes) -> Optional[tuple[int, bytes]]:
    """Parse OP_CONFIG_RESP (0x24) → (page_index, 50_bytes_data)."""
    if len(payload) < 2 or payload[0] != OP_CONFIG_RESP:
        return None
    page = payload[1]
    data = payload[2:2 + CONFIG_PAGE_SIZE]
    if len(data) != CONFIG_PAGE_SIZE:
        return None
    return page, bytes(data)


# ── Transport layer ───────────────────────────────────────────────────────────

class HIDTransport:
    """macOS/Windows/Linux USB HID transport using hidapi."""

    def __init__(self):
        self._dev = None

    def open(self) -> None:
        if not _HIDAPI:
            raise RuntimeError("hidapi not installed.  Run: pip install hidapi")
        devices = hid.enumerate(VID, PID)
        if not devices:
            raise OSError(f"DSP-408 not found (VID={VID:#06x} PID={PID:#06x}). "
                          "Check USB cable and power.")
        d = devices[0]
        self._dev = hid.device()
        self._dev.open_path(d['path'])
        # Keep blocking mode (nonblocking=False is the default).
        # On macOS, hidapi reads require the IOKit run loop to be pumped.
        # The read(size, timeout_ms) variant pumps the run loop internally
        # and is the only reliable way to receive IN reports on macOS.
        self._dev.set_nonblocking(False)
        # Drain any stale IN reports (short timeout, non-fatal if empty)
        for _ in range(5):
            if not self._dev.read(REPORT_SIZE, 30):
                break

    def close(self) -> None:
        if self._dev:
            self._dev.close()
            self._dev = None

    def send(self, frame: bytes) -> None:
        """Write a 64-byte frame to the device.

        hidapi on all platforms requires the report ID as the first byte
        of the write buffer.  For devices with no report IDs (like the
        DSP-408) that byte must be 0x00 — hidapi strips it before the
        64 raw bytes reach the USB endpoint.
        """
        assert len(frame) == REPORT_SIZE, f"frame must be {REPORT_SIZE} bytes, got {len(frame)}"
        self._dev.write(bytes([0x00]) + frame)

    def recv(self, timeout_ms: int = 500) -> Optional[bytes]:
        """Read one 64-byte IN report; return None on timeout.

        Uses read(size, timeout_ms) which pumps the macOS IOKit run loop
        internally — required for IN reports to arrive on macOS.
        """
        data = self._dev.read(REPORT_SIZE, timeout_ms)
        if data:
            return bytes(data)
        return None

    def info(self) -> str:
        if not self._dev:
            return "(not open)"
        mfr = self._dev.get_manufacturer_string() or ''
        prod = self._dev.get_product_string() or ''
        return f"{mfr} {prod}".strip() or f"VID={VID:#06x} PID={PID:#06x}"


class HIDRawTransport:
    """Linux /dev/hidraw* transport (non-exclusive, no hidapi needed)."""

    def __init__(self, path: str):
        self._path = path
        self._fd: Optional[int] = None

    def open(self) -> None:
        self._fd = os.open(self._path, os.O_RDWR)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def send(self, frame: bytes) -> None:
        assert len(frame) == REPORT_SIZE
        os.write(self._fd, frame)

    def recv(self, timeout_ms: int = 500) -> Optional[bytes]:
        r, _, _ = select.select([self._fd], [], [], timeout_ms / 1000.0)
        if not r:
            return None
        data = os.read(self._fd, REPORT_SIZE)
        return bytes(data) if data else None

    def info(self) -> str:
        return self._path


# ── High-level device class ───────────────────────────────────────────────────

class DSP408:
    """High-level DSP-408 controller."""

    def __init__(self, transport=None, hidraw_path: str = None):
        """
        transport:   HIDTransport or HIDRawTransport instance, or None for auto.
        hidraw_path: if given, use /dev/hidrawN directly (Linux).
        """
        if transport is not None:
            self._t = transport
        elif hidraw_path is not None:
            self._t = HIDRawTransport(hidraw_path)
        else:
            self._t = HIDTransport()
        self._initialized = False

    # --- Connection ---

    def open(self) -> None:
        """Open the device and run the initialization handshake."""
        self._t.open()
        print(f"Opened: {self._t.info()}")
        self._handshake()
        self._initialized = True

    def close(self) -> None:
        self._t.close()
        self._initialized = False

    def __enter__(self) -> 'DSP408':
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _handshake(self) -> None:
        """Send OP_INIT and consume the response (required before any command)."""
        print("  → OP_INIT (0x10) handshake...", end=' ')
        self._t.send(cmd_init())
        resp = self._t.recv(timeout_ms=800)
        if resp is None:
            print("no response (device may be off or frame format wrong)")
        else:
            payload = parse_frame(resp)
            print(f"OK  payload={payload.hex() if payload else resp[:8].hex()+'...'}")

    # --- Low-level send/recv ---

    def _send_recv(self, frame: bytes, label: str = '', timeout_ms: int = 500,
                   skip_polls: bool = False) -> Optional[bytes]:
        """Send a frame, return the parsed response payload, or None on timeout."""
        self._t.send(frame)
        for _ in range(10):
            raw = self._t.recv(timeout_ms)
            if raw is None:
                return None
            payload = parse_frame(raw)
            if payload is None:
                continue
            if skip_polls and payload and payload[0] == OP_POLL:
                continue  # skip unsolicited level reports while waiting
            return payload
        return None

    # --- High-level commands ---

    def get_firmware(self) -> Optional[str]:
        """Query firmware/model string (OP_FIRMWARE 0x13)."""
        p = self._send_recv(cmd_firmware(), 'firmware', skip_polls=True)
        if p and len(p) > 1:
            try:
                return p[1:].decode('ascii', errors='replace').rstrip('\x00').strip()
            except Exception:
                return p.hex()
        return None

    def get_device_info(self) -> Optional[dict]:
        """Query device info (OP_DEVICE_INFO 0x2C). Returns dict with 'locked' key."""
        p = self._send_recv(cmd_device_info(), 'device_info', skip_polls=True)
        if p is None or len(p) < 7 or p[0] != OP_DEVICE_INFO:
            return None
        return {'locked': p[6] == 0x01, 'raw': p.hex()}

    def poll(self) -> Optional[dict]:
        """Poll input/output levels (OP_POLL 0x40)."""
        p = self._send_recv(cmd_poll(), 'poll')
        return parse_levels(p) if p else None

    def activate(self) -> bool:
        """Send OP_ACTIVATE (0x12)."""
        p = self._send_recv(cmd_activate(), 'activate', skip_polls=True)
        return p is not None

    def read_config(self) -> Optional[bytes]:
        """Run manufacturer startup sequence and return stitched config bytes.

        Sequence: 0x13 → 0x2C → 0x22 → 0x14 → 30×0x29 → 9×0x27 → 0x12
        Returns 450 bytes (9 pages × 50 bytes), or None on failure.
        """
        print("  → firmware query (0x13)...", end=' ')
        fw = self.get_firmware()
        print(fw or "(no response)")

        print("  → device info (0x2C)...", end=' ')
        info = self.get_device_info()
        if info:
            print(f"locked={info['locked']}  raw={info['raw']}")
            if info['locked']:
                print("  ✗ Device is locked — submit PIN first")
                return None
        else:
            print("(no response)")

        print("  → preset header (0x22)...", end=' ')
        p = self._send_recv(cmd_preset_hdr(), 'preset_hdr', skip_polls=True)
        print(f"{'OK' if p else 'no response'}")

        print("  → preset index (0x14)...", end=' ')
        p = self._send_recv(cmd_preset_index(), 'preset_idx', skip_polls=True)
        active_slot = (p[1] if p and len(p) >= 2 else None)
        print(f"active slot = {active_slot}")

        print("  → reading 30 preset names (0x29)...")
        names: list[str] = []
        for slot in range(30):
            p = self._send_recv(cmd_read_name(slot), f'name_{slot}', skip_polls=True)
            if p and len(p) >= 16 and p[0] == OP_READ_NAME:
                name = p[2:16].decode('ascii', errors='replace').rstrip()
                names.append(name)
            else:
                names.append('')

        print(f"  → reading {CONFIG_PAGES} config pages (0x27)...")
        pages: dict[int, bytes] = {}
        for page in range(CONFIG_PAGES):
            p = self._send_recv(cmd_read_config(page), f'config_{page}', skip_polls=True)
            result = parse_config_page(p) if p else None
            if result is None:
                print(f"    page {page}: FAILED")
                return None
            idx, data = result
            pages[idx] = data
            print(f"    page {idx}: {len(data)} bytes OK")

        print("  → activate (0x12)...", end=' ')
        ok = self.activate()
        print("OK" if ok else "no response")

        # Stitch pages in order
        config = bytearray()
        for i in range(CONFIG_PAGES):
            config.extend(pages.get(i, bytes(CONFIG_PAGE_SIZE)))

        return {
            'config': bytes(config),
            'preset_names': names,
            'active_slot': active_slot,
        }

    # DSP control (all send + expect ACK response)

    def _ack(self, frame: bytes, label: str = '') -> bool:
        p = self._send_recv(frame, label, skip_polls=True)
        if p is None:
            print(f"  ✗ {label}: no response")
            return False
        if len(p) == 1 and p[0] == 0x01:
            return True
        # Some DSPs echo the command; tolerate non-standard ACK
        return True

    def set_gain(self, channel: int, raw: int) -> bool:
        """channel 0–3 inputs, 4–11 outputs.  raw 0–400 (dB = raw×0.1 − 28)."""
        return self._ack(cmd_gain(channel, raw), f'gain ch{channel}={raw}')

    def set_mute(self, channel: int, muted: bool) -> bool:
        return self._ack(cmd_mute(channel, muted), f'mute ch{channel}={muted}')

    def set_phase(self, channel: int, inverted: bool) -> bool:
        return self._ack(cmd_phase(channel, inverted), f'phase ch{channel}={inverted}')

    def set_delay(self, channel: int, samples: int) -> bool:
        """samples at 48 kHz; ms = samples / 48."""
        return self._ack(cmd_delay(channel, samples), f'delay ch{channel}={samples}')

    def set_lopass(self, channel: int, freq_raw: int, slope: int = 0x0A) -> bool:
        """slope: 0=bypass, 0x0A=LR-24 (device default)."""
        return self._ack(cmd_lopass(channel, freq_raw, slope), f'lopass ch{channel}')

    def set_hipass(self, channel: int, freq_raw: int, slope: int = 0x0A) -> bool:
        return self._ack(cmd_hipass(channel, freq_raw, slope), f'hipass ch{channel}')

    def set_peq(self, channel: int, band: int, gain_raw: int, freq_raw: int,
                q_raw: int, ftype: int = 0, bypass: bool = False) -> bool:
        return self._ack(cmd_peq(channel, band, gain_raw, freq_raw, q_raw, ftype, bypass),
                         f'peq ch{channel} band{band}')

    def set_matrix(self, output_ch: int, input_mask: int) -> bool:
        return self._ack(cmd_matrix(output_ch, input_mask), f'matrix out{output_ch}=0b{input_mask:04b}')

    def load_preset(self, slot: int) -> bool:
        """Load preset slot (0=F00, 1–30=U01–U30). Re-reads config internally."""
        p = self._send_recv(cmd_load_preset(slot), f'load_preset {slot}', skip_polls=True)
        return p is not None


# ── Unit conversion helpers ───────────────────────────────────────────────────

def db_to_gain_raw(db: float) -> int:
    """Convert dB to gain raw value.  Range: −28.0 dB (raw=0) to +12.0 dB (raw=400)."""
    raw = round((db + 28.0) * 10.0)
    return max(0, min(400, raw))

def gain_raw_to_db(raw: int) -> float:
    return raw * 0.1 - 28.0

def ms_to_samples(ms: float) -> int:
    """Convert milliseconds to 48 kHz sample count.  Max ≈ 680 ms."""
    return max(0, min(32640, round(ms * 48.0)))

def samples_to_ms(samples: int) -> float:
    return samples / 48.0


# ── CLI ───────────────────────────────────────────────────────────────────────

def _hexdump(data: bytes, cols: int = 16) -> str:
    lines = []
    for i in range(0, len(data), cols):
        chunk = data[i:i + cols]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'  {i:4x}: {hex_part:<{cols*3}}  {asc_part}')
    return '\n'.join(lines)


def cmd_test(args):
    """Quick connection and init test."""
    dsp = DSP408()
    try:
        dsp.open()
        print()
        print("Polling levels (OP_POLL 0x40)...")
        levels = dsp.poll()
        if levels:
            print(f"  Levels: {levels['channels']}")
        else:
            print("  (no response — try full init sequence with --init)")
        print()
        print("✓ Connection test complete")
    finally:
        dsp.close()


def cmd_init_seq(args):
    """Full initialization sequence + config read."""
    dsp = DSP408()
    try:
        dsp.open()
        print()
        result = dsp.read_config()
        if result is None:
            print("\n✗ Config read failed")
            return
        config = result['config']
        names = result['preset_names']
        slot = result['active_slot']
        print(f"\nActive preset slot: {slot}")
        print("Preset names:")
        for i, name in enumerate(names):
            if name.strip():
                print(f"  U{i+1:02d}: {name!r}")
        print(f"\nConfig ({len(config)} bytes):")
        print(_hexdump(config[:64]))
        if len(config) > 64:
            print(f"  ... ({len(config) - 64} more bytes)")
        print("\n✓ Init + config read complete")
    finally:
        dsp.close()


def cmd_gain_cli(args):
    """Set channel gain."""
    dsp = DSP408()
    try:
        dsp.open()
        raw = db_to_gain_raw(args.db)
        ok = dsp.set_gain(args.channel, raw)
        print(f"set_gain ch{args.channel} {args.db:+.1f} dB (raw={raw}): {'✓' if ok else '✗'}")
    finally:
        dsp.close()


def cmd_mute_cli(args):
    """Mute/unmute a channel."""
    dsp = DSP408()
    try:
        dsp.open()
        ok = dsp.set_mute(args.channel, args.mute)
        print(f"set_mute ch{args.channel} {'ON' if args.mute else 'OFF'}: {'✓' if ok else '✗'}")
    finally:
        dsp.close()


def cmd_poll_cli(args):
    """Continuous level poll."""
    dsp = DSP408()
    try:
        dsp.open()
        count = 0
        print("Polling (Ctrl-C to stop)...")
        while True:
            levels = dsp.poll()
            if levels:
                ch = levels['channels']
                bar = ' '.join(f'{v:3d}' for v in ch)
                print(f"\r  [{count:4d}] {bar}", end='', flush=True)
                count += 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        dsp.close()


def main():
    import argparse

    p = argparse.ArgumentParser(
        description='DSP-408 USB HID controller',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dsp408_hid.py test             # Quick connection test
  python dsp408_hid.py init             # Full init + config read
  python dsp408_hid.py gain 0 -6.0     # Set input 0 gain to -6 dB
  python dsp408_hid.py mute 4 on       # Mute output channel 4
  python dsp408_hid.py poll            # Continuous level display
""")
    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('test', help='Quick connection and init test')
    sub.add_parser('init', help='Full init sequence + config read')

    g = sub.add_parser('gain', help='Set channel gain')
    g.add_argument('channel', type=int, help='Channel 0–3 inputs, 4–11 outputs')
    g.add_argument('db', type=float, help='Gain in dB (−28.0 to +12.0)')

    m = sub.add_parser('mute', help='Mute/unmute channel')
    m.add_argument('channel', type=int)
    m.add_argument('mute', choices=['on', 'off', '1', '0'])

    sub.add_parser('poll', help='Continuous level display')

    args = p.parse_args()
    if args.cmd == 'test':
        cmd_test(args)
    elif args.cmd == 'init':
        cmd_init_seq(args)
    elif args.cmd == 'gain':
        cmd_gain_cli(args)
    elif args.cmd == 'mute':
        args.mute = args.mute in ('on', '1')
        cmd_mute_cli(args)
    elif args.cmd == 'poll':
        cmd_poll_cli(args)


if __name__ == '__main__':
    main()
