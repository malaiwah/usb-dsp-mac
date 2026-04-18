"""webui/app.py — Gradio web UI for the DSP-408, inspired by DSP-408.exe V1.24.

Launch (Linux / Pi):
    uv run python -m webui.app --host 0.0.0.0 --port 7860

Design scope — matches the Windows app's information architecture but
honest about what's not yet reverse-engineered:

  * Multi-device picker                — IMPLEMENTED (live, differs from
                                         Windows app which controls one)
  * Connection status banner           — IMPLEMENTED (live)
  * Device identity + preset name      — IMPLEMENTED (live)
  * Master volume slider               — placeholder (write path TBD)
  * 8-channel strips (gain/mute/delay/phase/source) — placeholder
  * 10-band PEQ graph + controls       — placeholder (needs 0x77NN decode)
  * High-pass / low-pass filters       — placeholder
  * Mixer 4×8 matrix                   — placeholder
  * Raw protocol console               — IMPLEMENTED (read/write any cmd)
  * Firmware flash / recovery          — IMPLEMENTED (life-saving)
  * State snapshot dump                — IMPLEMENTED

The placeholder controls are wired to real UI widgets with correct
ranges and labels from the manual, so once the 0x77NN layout is
decoded live on the Pi, only the serialization callback needs to
change — not the UI.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

try:
    import gradio as gr
except ImportError:
    print("gradio not installed. Run:  uv sync  (or: pip install gradio)")
    sys.exit(1)

# Make `dsp408` importable when running from the repo root
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dsp408 import (  # noqa: E402
    Device,
    DeviceNotFound,
    ProtocolError,
    enumerate_devices,
    resolve_selector,
)
from dsp408.flasher import flash_firmware  # noqa: E402
from dsp408.protocol import category_hint  # noqa: E402

# ── Domain constants (from DSP-408 manual) ─────────────────────────────
NUM_OUTPUT_CHANNELS = 8
NUM_INPUTS = 4
NUM_EQ_BANDS = 10
CROSSOVER_TYPES = ["Linkwitz-Riley", "Bessel", "Butterworth"]
CROSSOVER_SLOPES = [6, 12, 18, 24]  # dB/oct
FREQ_MIN, FREQ_MAX = 20, 20_000     # Hz
DELAY_UNITS = ["ms", "cm", "in"]
DELAY_MAX_CM = 277                  # manual says up to 277 cm
MASTER_VOLUME_MIN = -60             # dB (slider in UI)
MASTER_VOLUME_MAX = 0
CHANNEL_LEVEL_MIN = -60
CHANNEL_LEVEL_MAX = 6
EQ_GAIN_MIN = -12
EQ_GAIN_MAX = 12
Q_MIN, Q_MAX = 0.1, 10.0
BAND1_TYPES = ["PEQ", "LS"]         # low shelf only on band 1
BAND10_TYPES = ["PEQ", "HS"]        # high shelf only on band 10

# Default EQ bands (center freqs shown in the app screenshot)
DEFAULT_BAND_FREQS = [31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


# ── DeviceSession: single shared, lazily-opened connection ─────────────
class DeviceSession:
    """Lazy, thread-safe wrapper around one Device, with live device-switch support.

    The Gradio UI serves many simultaneous widget callbacks; they all go
    through the single lock inside Device, so we just need one shared
    instance that auto-reconnects on error. The `selector` is mutable
    so the top-of-page device picker can switch targets without
    restarting the server.

    Uses a reentrant lock because some call paths (e.g. reconnect →
    close → _ensure) re-enter locked regions on the same thread.
    Also exposes a `flashing` flag so the firmware tab can take
    exclusive control of the USB handle without racing with other
    widget callbacks.
    """

    def __init__(self):
        self._dev: Device | None = None
        self._lock = threading.RLock()
        self._flashing = False
        self._selector: str | None = None  # None = first found
        self.last_error: str | None = None

    # The "locked" helpers do the work assuming the caller holds _lock.
    def _ensure_locked(self) -> Device:
        if self._dev is not None:
            return self._dev
        self._dev = Device.open(selector=self._selector)
        self._dev.connect()
        return self._dev

    def _close_locked(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    @property
    def selector(self) -> str | None:
        with self._lock:
            return self._selector

    def set_selector(self, selector: str | None) -> None:
        """Switch which DSP-408 the session targets. Closes current handle."""
        with self._lock:
            if selector != self._selector:
                self._close_locked()
                self._selector = selector

    def current_device_path(self) -> bytes | None:
        with self._lock:
            return self._dev.path if self._dev else None

    def current_display_id(self) -> str | None:
        with self._lock:
            return self._dev.display_id if self._dev else None

    def reconnect(self) -> str:
        with self._lock:
            self._close_locked()
            try:
                dev = self._ensure_locked()
                return f"Connected — {dev.get_info()}"
            except Exception as e:
                self.last_error = str(e)
                return f"Failed: {e}"

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # Convenience wrapper: all widget callbacks go through this.
    def safe_call(self, fn, *args, **kwargs):
        """Run fn(device, *args, **kwargs), re-establishing the connection
        once if the first attempt fails. Blocks if a firmware flash is
        in progress (the flasher owns the USB handle exclusively)."""
        last: Exception | None = None
        for attempt in range(2):
            with self._lock:
                if self._flashing:
                    raise ProtocolError(
                        "firmware flash in progress — try again after it finishes"
                    )
                try:
                    dev = self._ensure_locked()
                    return fn(dev, *args, **kwargs)
                except (DeviceNotFound, ProtocolError, OSError) as e:
                    last = e
                    self._close_locked()
            time.sleep(0.2)
        assert last is not None
        self.last_error = str(last)
        raise last

    # ----- firmware-flash exclusive critical section ---------------------
    @contextmanager
    def flash_lock(self):
        """Acquire exclusive USB access for a firmware flash.

        Holds the session lock for the entire duration of the flash so
        no other widget callback can concurrently open the device
        handle. Closes any existing Device before yielding.
        """
        with self._lock:
            self._close_locked()
            self._flashing = True
            try:
                yield
            finally:
                self._flashing = False


SESSION = DeviceSession()


# ── Device-picker helpers ──────────────────────────────────────────────
def _picker_choices() -> list[str]:
    """Return the labels shown in the device dropdown.

    Label format: `[<index>] <friendly_or_display_id>[ · <display_id>]`
    When an alias is configured, the friendly name leads and the stable
    display_id is shown after a separator so the user can still see the
    machine id. The parser in `_label_to_selector` picks the string
    between `]` and ` · ` — which is either the friendly name (passed
    to `resolve_selector` → matches on friendly_name) or the display_id.
    """
    out = []
    for d in enumerate_devices():
        name = d.get("friendly_name") or d["display_id"]
        label = f"[{d['index']}] {name}"
        if name != d["display_id"]:
            label += f" · {d['display_id']}"
        elif d.get("product_string") and d["product_string"] != d["display_id"]:
            label += f" · {d['product_string']}"
        out.append(label)
    return out


def _label_to_selector(label: str) -> str | None:
    """Convert a dropdown label back to the stable selector string."""
    # Format: "[<index>] <name>[ · <tail>]"
    if not label:
        return None
    rest = label.split("]", 1)
    if len(rest) != 2:
        return label
    tail = rest[1].strip()
    if " · " in tail:
        tail = tail.split(" · ", 1)[0]
    return tail.strip() or None


def do_refresh_devices():
    """Re-enumerate USB and update the picker dropdown choices."""
    choices = _picker_choices()
    if not choices:
        return gr.update(choices=[], value=None), "No DSP-408 attached."
    # Keep current selection if still present
    sel = SESSION.selector
    current_label = None
    for c in choices:
        if _label_to_selector(c) == sel:
            current_label = c
            break
    if current_label is None:
        current_label = choices[0]
    return (
        gr.update(choices=choices, value=current_label),
        f"{len(choices)} device(s) attached.",
    )


def do_pick_device(label: str):
    """User selected a different DSP-408 from the dropdown."""
    sel = _label_to_selector(label)
    SESSION.set_selector(sel)
    return do_connect()


# ── Callback helpers ───────────────────────────────────────────────────
def safe_text(fn, *args, error_prefix="error") -> str:
    """Run a callback and turn exceptions into friendly messages."""
    try:
        return fn(*args)
    except DeviceNotFound:
        return f"{error_prefix}: no DSP-408 detected (check USB cable)"
    except ProtocolError as e:
        return f"{error_prefix}: protocol — {e}"
    except Exception as e:  # pragma: no cover
        return f"{error_prefix}: {e}\n\n{traceback.format_exc()}"


def do_connect() -> tuple[str, str, str]:
    """Returns (banner_html, identity, preset_name)."""
    try:
        info = SESSION.safe_call(lambda d: d.snapshot())
        did = SESSION.current_display_id() or "?"
    except DeviceNotFound:
        return (
            '<span style="color:#c33">● Not connected — no DSP-408 on USB</span>',
            "",
            "",
        )
    except Exception as e:
        return (f'<span style="color:#c33">● Error: {e}</span>', "", "")

    banner = (
        f'<span style="color:#2a2">● Connected — {info.identity} '
        f'<span style="color:#888">({did})</span></span>'
    )
    return banner, info.identity, info.preset_name


def do_snapshot_dump() -> str:
    try:
        info = SESSION.safe_call(lambda d: d.snapshot())
    except Exception as e:
        return f"error: {e}"
    return (
        f"identity      : {info.identity}\n"
        f"preset name   : {info.preset_name}\n"
        f"status byte   : 0x{info.status_byte:02x}\n"
        f"state 0x13    : {info.state_13.hex(' ')}\n"
        f"global 0x02   : {info.global_02.hex(' ')}\n"
        f"global 0x05   : {info.global_05.hex(' ')}\n"
        f"global 0x06   : {info.global_06.hex(' ')}"
    )


def do_raw_read(cmd_hex: str, cat_hex: str) -> str:
    try:
        cmd = int(cmd_hex, 16)
        cat_str = (cat_hex or "").strip().lower()
        cat = category_hint(cmd) if cat_str in ("", "auto") else int(cat_str, 16)
    except ValueError:
        return "bad hex"
    try:
        reply = SESSION.safe_call(
            lambda d: d.read_raw(cmd=cmd, category=cat, timeout_ms=3000)
        )
    except Exception as e:
        return f"error: {e}"
    return (
        f"cmd=0x{reply.cmd:04x} cat=0x{reply.category:02x} "
        f"dir=0x{reply.direction:02x} seq={reply.seq} "
        f"len={reply.payload_len} chk_ok={reply.checksum_ok}\n\n"
        f"payload ({len(reply.payload)} bytes):\n{reply.payload.hex(' ')}"
    )


def do_raw_write(cmd_hex: str, hex_payload: str, cat_hex: str) -> str:
    try:
        cmd = int(cmd_hex, 16)
        cat_str = (cat_hex or "").strip().lower()
        cat = category_hint(cmd) if cat_str in ("", "auto") else int(cat_str, 16)
        payload = bytes.fromhex(hex_payload.replace(" ", "").replace(",", ""))
    except ValueError as e:
        return f"bad input: {e}"
    try:
        reply = SESSION.safe_call(
            lambda d: d.write_raw(cmd=cmd, data=payload, category=cat)
        )
    except Exception as e:
        return f"error: {e}"
    return (
        f"ack dir=0x{reply.direction:02x} cat=0x{reply.category:02x} "
        f"seq={reply.seq} len={reply.payload_len}"
    )


def do_read_channel(channel: int) -> str:
    try:
        data = SESSION.safe_call(lambda d: d.read_channel_state(int(channel)))
    except Exception as e:
        return f"error: {e}"
    # Pretty-print 16 bytes per row
    rows = []
    for i in range(0, len(data), 16):
        rows.append(f"{i:04x}  {data[i:i+16].hex(' ')}")
    return f"channel {channel}: {len(data)} bytes\n\n" + "\n".join(rows)


def do_rename_preset(new_name: str) -> str:
    name = (new_name or "").strip()
    if not name:
        return "empty name"
    try:
        SESSION.safe_call(lambda d: d.write_preset_name(name))
    except Exception as e:
        return f"error: {e}"
    return f"renamed preset to {name!r}"


# ── Firmware flashing ──────────────────────────────────────────────────
def do_flash(fw_file) -> str:
    """Flash the selected .bin firmware onto the currently-picked device.

    Holds SESSION.flash_lock() for the entire upload so no other widget
    callback can steal the USB handle mid-flash (this is a hard EBUSY
    on Linux hidraw and silently steals the handle on macOS).
    """
    if fw_file is None:
        return "no firmware file selected"
    # Gradio File component yields a temp path
    path = Path(fw_file.name) if hasattr(fw_file, "name") else Path(str(fw_file))
    if not path.exists():
        return f"file not found: {path}"

    log: list[str] = []

    def progress(cur: int, total: int, label: str) -> None:
        log.append(f"[{label:>22}] {cur}/{total}")

    # Resolve device path so we target the currently-picked device (not
    # just the first on the bus).
    target_path = SESSION.current_device_path()
    if target_path is None:
        try:
            target_path = resolve_selector(
                SESSION.selector, enumerate_devices()
            )["path"]
        except DeviceNotFound as e:
            return f"error: {e}"

    with SESSION.flash_lock():
        try:
            flash_firmware(path, progress=progress, device_path=target_path)
        except Exception as e:
            log.append(f"ERROR: {e}")
            return "\n".join(log)
    log.append("OK — device rebooting, will re-enumerate in ~20 s.")
    return "\n".join(log)


# ── UI construction ────────────────────────────────────────────────────
def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="DSP-408 Control",
        theme=gr.themes.Soft(),
    ) as demo:
        gr.Markdown(
            "# DSP-408 Control\n"
            "Dayton Audio DSP-408 — 4 inputs × 8 outputs, 10-band PEQ per "
            "channel. Supports **multiple DSP-408s at once** (pick which "
            "one you're editing from the Device dropdown). Use the Raw "
            "Console to experiment with any command live."
        )

        # ── Device picker (multi-device) ──────────────────────────
        initial_choices = _picker_choices()
        with gr.Row():
            device_picker = gr.Dropdown(
                choices=initial_choices,
                value=initial_choices[0] if initial_choices else None,
                label="Device",
                interactive=True,
                scale=3,
            )
            refresh_btn = gr.Button("↻ Refresh devices", scale=1)
            picker_status = gr.Markdown(
                f"{len(initial_choices)} device(s) attached."
                if initial_choices else "No DSP-408 attached.",
            )

        # Banner + connect
        with gr.Row():
            banner = gr.HTML(
                '<span style="color:#888">○ Not connected</span>',
                label="Status",
            )
            connect_btn = gr.Button("Connect / Reconnect", variant="primary")

        # Header info
        with gr.Row():
            identity_box = gr.Textbox(
                label="Device identity", interactive=False
            )
            preset_box = gr.Textbox(label="Active preset name")
            rename_btn = gr.Button("Rename preset")
        rename_log = gr.Textbox(label="Rename result", interactive=False)

        # Wire picker
        refresh_btn.click(
            fn=do_refresh_devices,
            outputs=[device_picker, picker_status],
        )
        device_picker.change(
            fn=do_pick_device,
            inputs=device_picker,
            outputs=[banner, identity_box, preset_box],
        )

        connect_btn.click(
            fn=do_connect, outputs=[banner, identity_box, preset_box]
        )
        rename_btn.click(
            fn=do_rename_preset, inputs=preset_box, outputs=rename_log
        )

        # ── Tabs ──────────────────────────────────────────────────────
        with gr.Tabs():
            # ── Channels tab (placeholder; needs 0x77NN decode) ───
            with gr.Tab("Channels"):
                gr.Markdown(
                    "Each output channel has 10-band PEQ + HPF + LPF + "
                    "level + mute + phase + delay. The UI below is a "
                    "placeholder — the 296-byte 0x77NN response layout "
                    "is not yet decoded, so these controls are not live "
                    "wired yet. Use the **Raw Console** for now."
                )
                with gr.Row():
                    gr.Slider(
                        MASTER_VOLUME_MIN, MASTER_VOLUME_MAX, value=-20,
                        step=1, label="Master volume (dB) — placeholder",
                    )
                for ch in range(1, NUM_OUTPUT_CHANNELS + 1):
                    with gr.Accordion(f"CH{ch}", open=(ch == 1)):
                        with gr.Row():
                            gr.Slider(
                                CHANNEL_LEVEL_MIN, CHANNEL_LEVEL_MAX, value=0,
                                step=0.1, label="Level (dB)",
                            )
                            gr.Checkbox(label="Mute")
                            gr.Radio(
                                ["0°", "180°"], value="0°", label="Phase"
                            )
                            gr.Number(label="Delay (cm)", value=0,
                                      minimum=0, maximum=DELAY_MAX_CM,
                                      step=1)
                        gr.CheckboxGroup(
                            [f"IN{i+1}" for i in range(NUM_INPUTS)],
                            label="Source (mixer cell on/off)",
                            value=[f"IN{(ch-1) % NUM_INPUTS + 1}"],
                        )
                        with gr.Row():
                            gr.Dropdown(
                                CROSSOVER_TYPES,
                                value="Linkwitz-Riley",
                                label="HPF type",
                            )
                            gr.Number(label="HPF freq (Hz)", value=20,
                                      minimum=FREQ_MIN, maximum=FREQ_MAX)
                            gr.Dropdown(
                                [f"{s} dB/Oct" for s in CROSSOVER_SLOPES],
                                value="12 dB/Oct",
                                label="HPF slope",
                            )
                        with gr.Row():
                            gr.Dropdown(
                                CROSSOVER_TYPES,
                                value="Linkwitz-Riley",
                                label="LPF type",
                            )
                            gr.Number(label="LPF freq (Hz)", value=FREQ_MAX,
                                      minimum=FREQ_MIN, maximum=FREQ_MAX)
                            gr.Dropdown(
                                [f"{s} dB/Oct" for s in CROSSOVER_SLOPES],
                                value="12 dB/Oct",
                                label="LPF slope",
                            )
                        gr.Markdown("**10-band PEQ**")
                        for i, f in enumerate(DEFAULT_BAND_FREQS, start=1):
                            with gr.Row():
                                gr.Number(label=f"Band {i} Freq (Hz)", value=f,
                                          minimum=FREQ_MIN, maximum=FREQ_MAX)
                                gr.Number(label="Q", value=2.515,
                                          minimum=Q_MIN, maximum=Q_MAX,
                                          step=0.001)
                                gr.Slider(EQ_GAIN_MIN, EQ_GAIN_MAX, value=0,
                                          step=0.1, label="Gain (dB)")
                                if i == 1:
                                    gr.Radio(BAND1_TYPES, value="PEQ",
                                             label="Type")
                                elif i == 10:
                                    gr.Radio(BAND10_TYPES, value="PEQ",
                                             label="Type")

            # ── Mixer tab (placeholder) ───────────────────────────
            with gr.Tab("Mixer"):
                gr.Markdown(
                    "4 inputs × 8 outputs routing matrix — placeholder."
                )
                # A simple grid of sliders
                for out_ch in range(1, NUM_OUTPUT_CHANNELS + 1):
                    with gr.Row():
                        gr.Markdown(f"**CH{out_ch} ←**")
                        for in_ch in range(1, NUM_INPUTS + 1):
                            gr.Slider(
                                0, 100, value=100 if in_ch == ((out_ch - 1) % 4) + 1 else 0,
                                step=1, label=f"IN{in_ch}",
                            )

            # ── Snapshot / diagnostics ────────────────────────────
            with gr.Tab("Snapshot"):
                dump = gr.Textbox(
                    label="startup handshake dump",
                    lines=10, interactive=False,
                )
                snap_btn = gr.Button("Take snapshot")
                snap_btn.click(fn=do_snapshot_dump, outputs=dump)

                gr.Markdown("### Read channel state (0x77NN, 296 bytes raw)")
                with gr.Row():
                    ch_num = gr.Slider(
                        0, 7, value=0, step=1,
                        label="Channel (0..7)",
                    )
                    ch_read_btn = gr.Button("Read")
                ch_out = gr.Textbox(label="raw bytes", lines=12,
                                    interactive=False)
                ch_read_btn.click(fn=do_read_channel,
                                  inputs=ch_num, outputs=ch_out)

            # ── Raw console ───────────────────────────────────────
            with gr.Tab("Raw Console"):
                gr.Markdown(
                    "Send any 80-80-80-ee-framed command. "
                    "Category byte 0x09 for state commands (connect, info, "
                    "preset, firmware), 0x04 for parameter commands "
                    "(0x77NN, 0x1fNN, 0x2000). Leave category blank / "
                    "\"auto\" to pick automatically from the cmd code."
                )
                with gr.Row():
                    read_cmd = gr.Textbox(value="04", label="cmd (hex)")
                    read_cat = gr.Textbox(value="auto", label="category (hex or 'auto')")
                    read_btn = gr.Button("READ (dir=a2)")
                read_out = gr.Textbox(label="reply", lines=8, interactive=False)
                read_btn.click(fn=do_raw_read,
                               inputs=[read_cmd, read_cat],
                               outputs=read_out)

                gr.Markdown("---")
                with gr.Row():
                    write_cmd = gr.Textbox(value="1f07", label="cmd (hex)")
                    write_cat = gr.Textbox(value="auto", label="category (hex or 'auto')")
                write_payload = gr.Textbox(
                    value="01 00 00 00 00 00 00 12",
                    label="payload (hex bytes, spaces ok)",
                )
                write_btn = gr.Button("WRITE (dir=a1)")
                write_out = gr.Textbox(label="ack", lines=3, interactive=False)
                write_btn.click(fn=do_raw_write,
                                inputs=[write_cmd, write_payload, write_cat],
                                outputs=write_out)

            # ── Firmware flash / recovery ─────────────────────────
            with gr.Tab("Firmware"):
                gr.Markdown(
                    "## Firmware flash / recovery\n\n"
                    "**Use this if the Windows app or your custom firmware "
                    "has broken device detection.** The flasher bypasses "
                    "HID Usage Page matching and talks to the device by "
                    "VID/PID, so it can recover a unit flashed with a "
                    "patched HID descriptor.\n\n"
                    "Flashing targets the device currently selected in "
                    "the **Device** dropdown at the top of the page. "
                    "Expect ~2 minutes (trigger → prep → 1465 blocks → "
                    "apply + reboot). Do NOT unplug during the upload."
                )
                fw_file = gr.File(
                    label=".bin firmware (original or patched)",
                    file_types=[".bin"],
                )
                flash_btn = gr.Button("Flash firmware", variant="stop")
                flash_log = gr.Textbox(
                    label="progress", lines=14, interactive=False,
                    max_lines=40,
                )
                flash_btn.click(fn=do_flash, inputs=fw_file,
                                outputs=flash_log)

        # Auto-connect on page load
        demo.load(fn=do_connect,
                  outputs=[banner, identity_box, preset_box])

    return demo


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default=7860, type=int)
    ap.add_argument("--share", action="store_true")
    args = ap.parse_args()

    demo = build_ui()
    demo.launch(server_name=args.host, server_port=args.port, share=args.share)


if __name__ == "__main__":
    main()
