"""dsp408 — Dayton Audio DSP-408 USB control library.

Implements the `80 80 80 ee` HID transport protocol reverse-engineered from
Windows USBPcap captures of the official DSP-408.exe V1.24 GUI.

Top-level API:

    from dsp408 import Device, DeviceNotFound

    with Device.open() as dev:
        dev.connect()
        print(dev.get_info())            # "MYDW-AV1.06"
        print(dev.read_preset_name())    # e.g. "test"
        raw = dev.read_channel_state(0)  # 296 bytes of channel 1 params

See `dsp408.protocol` for frame-level constants, and `dsp408.flasher` for
firmware upload. This library is cross-platform (hidapi), designed to
run on Linux (Raspberry Pi) and macOS; first live bring-up is planned
against the DSP-408 attached to a Raspberry Pi.

Device specs (from the manual):
  * 4 RCA + 4 high-level inputs
  * 8 RCA outputs
  * 10-band PEQ per output channel (bands 1 & 10 can be shelf LS/HS)
  * Independent HPF + LPF per channel, types: Linkwitz-Riley / Bessel /
    Butterworth, slopes: 6/12/18/24 dB/oct, freq 20 Hz – 20 kHz
  * Per-channel delay — wire format is samples (u16). Firmware caps at
    359 taps: that's 8.14 ms @ 44.1 kHz (matching the manual's "8.1471 ms /
    277 cm" claim) but only 7.48 ms when the device runs at 48 kHz.
  * 4×8 input→output mixer matrix
  * 6 named presets (save / load / recall / delete)
  * Master volume + per-channel mute + per-channel phase invert
"""

from .config import (
    default_search_paths,
    friendly_name_for,
    load_aliases,
)
from .device import (
    Device,
    DeviceInfo,
    DeviceNotFound,
    ProtocolError,
    enumerate_devices,
    resolve_selector,
)
from .protocol import (
    DIR_CMD,
    DIR_RESP,
    DIR_WRITE,
    DIR_WRITE_ACK,
    FRAME_MAGIC,
    PID,
    VID,
    build_frame,
    category_hint,
    parse_frame,
    xor_checksum,
)

__all__ = [
    "VID",
    "PID",
    "FRAME_MAGIC",
    "DIR_CMD",
    "DIR_RESP",
    "DIR_WRITE",
    "DIR_WRITE_ACK",
    "build_frame",
    "category_hint",
    "parse_frame",
    "xor_checksum",
    "Device",
    "DeviceInfo",
    "DeviceNotFound",
    "ProtocolError",
    "enumerate_devices",
    "resolve_selector",
    "load_aliases",
    "friendly_name_for",
    "default_search_paths",
]

__version__ = "0.1.0"
