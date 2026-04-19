"""dsp408.mqtt — Home Assistant MQTT discovery bridge (multi-device).

Publishes a single *device-based* discovery document per DSP-408 found on
the USB bus, using the MQTT discovery format supported by Home Assistant
2024.12+ (abbreviated keys, one config topic with many `cmps`).

Topic tree (for one DSP-408 identified by its `display_id`, e.g.
`MYDW-AV1.06` or `dsp408-a1b2c3d4`):

    homeassistant/device/dsp408_<id>/config   — HA discovery (retained)
    dsp408/bridge/status                      — bridge-level LWT (retained)
    dsp408/<id>/status                        — per-device availability (retained)
    dsp408/<id>/identity/state                — RO sensor
    dsp408/<id>/preset/state                  — RW text
    dsp408/<id>/preset/set                    — cmd topic
    dsp408/<id>/status_byte/state             — RO sensor (numeric)
    dsp408/<id>/state_0x13/state              — RO diagnostics sensor (hex blob)
    dsp408/<id>/global_06/state               — RO diagnostics sensor (hex blob)
    dsp408/<id>/raw/read      (cmd JSON {"cmd":..., "cat":...})
    dsp408/<id>/raw/read/reply  (JSON reply)
    dsp408/<id>/raw/write     (cmd JSON {"cmd":..., "cat":..., "data_hex":...})
    dsp408/<id>/raw/write/ack  (JSON ack)

Per-device `avty` is inherited by all components under 2024.12+; the
bridge-level LWT adds a second availability entry (pl_avail/pl_not_avail)
so that if the bridge process dies *every* device (not just the first)
flips to unavailable in HA immediately.

Most per-channel audio controls (gain / mute / phase / delay / EQ) are
not exposed yet because the 0x77NN layout is not decoded. Once the
layout is known, add them to `DeviceWorker.build_discovery_payload()`.

Requires `paho-mqtt` (install with `uv sync --extra mqtt`).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass

try:
    import paho.mqtt.client as mqtt
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "paho-mqtt is required for dsp408.mqtt. "
        "Install with: uv sync --extra mqtt  (or: pip install paho-mqtt)"
    ) from e

from . import Device, DeviceNotFound, ProtocolError, enumerate_devices, resolve_selector
from .protocol import category_hint

log = logging.getLogger("dsp408.mqtt")

# ── HA discovery schema helpers ────────────────────────────────────────
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_]+")


def sanitize_id(s: str) -> str:
    """Turn an arbitrary string into a valid HA unique_id / MQTT slug.

    HA allows lowercase, digits, underscore, hyphen in unique_id; MQTT
    topics forbid `+`, `#`, `/`. We collapse any non-[A-Za-z0-9_] run
    into a single underscore and lowercase the result.
    """
    s = _SAFE_ID_RE.sub("_", s or "").strip("_").lower()
    return s or "dsp408"


def _rc_is_success(rc) -> bool:
    """Return True if a paho-mqtt connect/publish return code is success.

    paho v1: `rc` is an int (0 = success). `int(rc) == 0`.
    paho v2: `rc` is a `ReasonCode` object. It does NOT implement
    `__int__`, so `int(rc)` raises TypeError. Use `.is_failure` or
    the `.value` attribute instead.
    """
    # v2 ReasonCode
    if hasattr(rc, "is_failure"):
        return not rc.is_failure
    if hasattr(rc, "value"):
        return int(rc.value) == 0
    # v1 plain int
    try:
        return int(rc) == 0
    except (TypeError, ValueError):
        return False


@dataclass
class BridgeConfig:
    broker: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "dsp408"
    discovery_prefix: str = "homeassistant"
    poll_interval: float = 2.0
    selector: str | None = None  # None = all devices; str/int = just one
    aliases: dict[str, str] | None = None  # device-id → friendly name


def bridge_status_topic(base_topic: str) -> str:
    """The bridge-level LWT topic. Used as a secondary `avty` entry on
    every device so that a bridge crash flips all devices to unavailable."""
    return f"{base_topic}/bridge/status"


# ── Per-device worker ──────────────────────────────────────────────────
class DeviceWorker:
    """Owns one Device handle + its MQTT pub/sub for one DSP-408.

    Runs a background thread that periodically polls state and publishes
    it, and handles inbound commands from HA by dispatching them to the
    same Device (so USB I/O is serialized inside Device's own lock).
    """

    def __init__(
        self,
        client: mqtt.Client,
        info: dict,
        cfg: BridgeConfig,
    ):
        self._client = client
        self._cfg = cfg
        self._info = info
        self.slug = sanitize_id(info["display_id"])
        self._base = f"{cfg.base_topic}/{self.slug}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._dev: Device | None = None
        self._identity_cached: str = ""
        self._online_flag: bool = False  # reflects last availability we published

    # ── topic helpers ───────────────────────────────────────────────
    def t(self, suffix: str) -> str:
        return f"{self._base}/{suffix}"

    def availability_topic(self) -> str:
        return self.t("status")

    def discovery_topic(self) -> str:
        return f"{self._cfg.discovery_prefix}/device/dsp408_{self.slug}/config"

    # ── device handle lifecycle ─────────────────────────────────────
    def _ensure_device(self) -> Device:
        with self._lock:
            if self._dev is None:
                # Open by stable path so we survive a re-enumeration loop
                self._dev = Device.open(path=self._info["path"])
                self._dev.connect()
            return self._dev

    def _close_device(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:
                    pass
                self._dev = None

    # ── HA discovery doc ────────────────────────────────────────────
    def build_discovery_payload(self) -> dict:
        """Build the device-based HA discovery payload (one JSON doc).

        Uses the HA 2024.12+ short-key format: `dev` (device), `o`
        (origin), `avty` (availability list), `cmps` (components).
        Components inherit top-level `avty` so each entry does NOT need
        its own availability block.
        """
        serial = self._info.get("serial_number") or self._info["display_id"]
        product = self._info.get("product_string") or "DSP-408"
        manufacturer = self._info.get("manufacturer") or "Dayton Audio"
        # Prefer the user's friendly name (from ~/.config/dsp408/aliases.toml)
        # when available; fall back to the machine-generated display_id.
        display = (self._info.get("friendly_name")
                   or self._info.get("display_id")
                   or "DSP-408")
        is_aliased = display != self._info.get("display_id")

        dev = {
            # HA abbreviations:
            "ids": [f"dsp408_{self.slug}"],
            # If the user gave the device a friendly name, show just that
            # (cleaner in HA). Otherwise, fall back to the old
            # "DSP-408 (<display_id>)" format for disambiguation.
            "name": display if is_aliased else f"DSP-408 ({display})",
            "mf": manufacturer,
            "mdl": product,
            "sn": serial,
            "sw": self._identity_cached or "unknown",
        }
        # Two-entry availability list: per-device + bridge-level LWT.
        # `avty_mode: all` means both must be "online" for the device to
        # show as available — HA's default is "latest" but for us both
        # signals must agree.
        avty = [
            {
                "t": self.availability_topic(),
                "pl_avail": "online",
                "pl_not_avail": "offline",
            },
            {
                "t": bridge_status_topic(self._cfg.base_topic),
                "pl_avail": "online",
                "pl_not_avail": "offline",
            },
        ]
        # `cmps` = component map. Keys are our own ids, values contain
        # component type under `p` + component-specific fields.
        cmps: dict = {
            # Device identity / firmware (sensor, RO)
            "identity": {
                "p": "sensor",
                "name": "Firmware identity",
                "uniq_id": f"dsp408_{self.slug}_identity",
                "stat_t": self.t("identity/state"),
                "ent_cat": "diagnostic",
                "icon": "mdi:chip",
            },
            # Preset name (text, RW)
            "preset": {
                "p": "text",
                "name": "Preset name",
                "uniq_id": f"dsp408_{self.slug}_preset",
                "stat_t": self.t("preset/state"),
                "cmd_t": self.t("preset/set"),
                "max": 15,
                "icon": "mdi:pencil",
            },
            # Status byte (numeric sensor, RO, diagnostic)
            "status_byte": {
                "p": "sensor",
                "name": "Status byte",
                "uniq_id": f"dsp408_{self.slug}_status",
                "stat_t": self.t("status_byte/state"),
                "ent_cat": "diagnostic",
                "stat_cla": "measurement",
            },
            # State 0x13 blob (sensor, RO, diagnostic)
            "state_0x13": {
                "p": "sensor",
                "name": "State 0x13",
                "uniq_id": f"dsp408_{self.slug}_state_0x13",
                "stat_t": self.t("state_0x13/state"),
                "ent_cat": "diagnostic",
                "icon": "mdi:tune-vertical",
            },
            "global_06": {
                "p": "sensor",
                "name": "Global 0x06",
                "uniq_id": f"dsp408_{self.slug}_global_06",
                "stat_t": self.t("global_06/state"),
                "ent_cat": "diagnostic",
            },
            # Master volume slider (-60..+6 dB, 1 dB step)
            "master_volume": {
                "p": "number",
                "name": "Master volume",
                "uniq_id": f"dsp408_{self.slug}_master_volume",
                "stat_t": self.t("master_volume/state"),
                "cmd_t": self.t("master_volume/set"),
                "min": -60,
                "max": 6,
                "step": 1,
                "unit_of_meas": "dB",
                "icon": "mdi:volume-high",
                "mode": "slider",
            },
            # Master mute switch
            "master_mute": {
                "p": "switch",
                "name": "Master mute",
                "uniq_id": f"dsp408_{self.slug}_master_mute",
                "stat_t": self.t("master_mute/state"),
                "cmd_t": self.t("master_mute/set"),
                "pl_on": "ON",
                "pl_off": "OFF",
                "icon": "mdi:volume-off",
            },
            # Per-channel volume + mute (8 each)
            **{
                f"ch{n}_volume": {
                    "p": "number",
                    "name": f"Channel {n} volume",
                    "uniq_id": f"dsp408_{self.slug}_ch{n}_volume",
                    "stat_t": self.t(f"ch{n}_volume/state"),
                    "cmd_t": self.t(f"ch{n}_volume/set"),
                    "min": -60,
                    "max": 0,
                    "step": 0.5,
                    "unit_of_meas": "dB",
                    "icon": "mdi:tune-vertical-variant",
                    "mode": "slider",
                }
                for n in range(1, 9)
            },
            **{
                f"ch{n}_mute": {
                    "p": "switch",
                    "name": f"Channel {n} mute",
                    "uniq_id": f"dsp408_{self.slug}_ch{n}_mute",
                    "stat_t": self.t(f"ch{n}_mute/state"),
                    "cmd_t": self.t(f"ch{n}_mute/set"),
                    "pl_on": "ON",
                    "pl_off": "OFF",
                    "icon": "mdi:volume-mute",
                }
                for n in range(1, 9)
            },
            # Per-channel phase invert (polar) switches. byte[1] of cmd=0x1FNN.
            # Validated live: tests/loopback/test_phase_invert.py.
            **{
                f"ch{n}_polar": {
                    "p": "switch",
                    "name": f"Channel {n} phase invert",
                    "uniq_id": f"dsp408_{self.slug}_ch{n}_polar",
                    "stat_t": self.t(f"ch{n}_polar/state"),
                    "cmd_t": self.t(f"ch{n}_polar/set"),
                    "pl_on": "ON",
                    "pl_off": "OFF",
                    "icon": "mdi:sine-wave",
                    "ent_cat": "config",
                }
                for n in range(1, 9)
            },
            # Per-channel delay (samples). Firmware caps at 359 taps.
            # Validated live: tests/loopback/test_delay_calibration.py.
            **{
                f"ch{n}_delay": {
                    "p": "number",
                    "name": f"Channel {n} delay",
                    "uniq_id": f"dsp408_{self.slug}_ch{n}_delay",
                    "stat_t": self.t(f"ch{n}_delay/state"),
                    "cmd_t": self.t(f"ch{n}_delay/set"),
                    "min": 0,
                    "max": 359,
                    "step": 1,
                    "unit_of_meas": "samples",
                    "icon": "mdi:timer-outline",
                    "mode": "slider",
                    "ent_cat": "config",
                }
                for n in range(1, 9)
            },
            # Per-output input-routing switches: 8 outs × 4 ins = 32 switches.
            # `out{N}_in{M}` toggles whether IN<M> feeds Out<N>.
            **{
                f"out{n}_in{m}": {
                    "p": "switch",
                    "name": f"Out {n} ← In {m}",
                    "uniq_id": f"dsp408_{self.slug}_out{n}_in{m}",
                    "stat_t": self.t(f"route/out{n}_in{m}/state"),
                    "cmd_t": self.t(f"route/out{n}_in{m}/set"),
                    "pl_on": "ON",
                    "pl_off": "OFF",
                    "icon": "mdi:call-merge",
                    "ent_cat": "config",
                }
                for n in range(1, 9)
                for m in range(1, 5)
            },
            # ── ⚠ KNOWN-BROKEN: factory-preset / reset buttons ────────
            # Wire encoding for the magic-word system-register write is
            # not yet determined. Live probing showed the cmd is either
            # silently ignored or routed into the wrong subsystem (one
            # candidate landed inside an EQ band and corrupted it).
            # Buttons are kept as call-site probes so a future capture
            # of the official app can be slotted in directly. Hidden
            # under the diagnostic category so they're tucked out of the
            # main UI — a user has to actively go look for them.
            "factory_reset": {
                "p": "button",
                "name": "Factory reset (BROKEN — encoding TBD)",
                "uniq_id": f"dsp408_{self.slug}_factory_reset",
                "cmd_t": self.t("system/factory_reset/press"),
                "icon": "mdi:restart-alert",
                "ent_cat": "diagnostic",
            },
            **{
                f"load_preset_{n}": {
                    "p": "button",
                    "name": f"Load factory preset {n} (BROKEN — encoding TBD)",
                    "uniq_id": f"dsp408_{self.slug}_load_preset_{n}",
                    "cmd_t": self.t(f"system/load_preset/{n}/press"),
                    "icon": "mdi:speaker-multiple",
                    "ent_cat": "diagnostic",
                }
                for n in range(1, 7)
            },
            # Per-cell routing level (u8 0..255). Lets the user dial in a
            # non-unity mix or boost (0xFF ≈ +8.1 dB headroom, validated by
            # tests/loopback/test_routing_percentage.py). The bool switch
            # above is a thin wrapper: ON = 0x64 (unity), OFF = 0x00.
            **{
                f"out{n}_in{m}_level": {
                    "p": "number",
                    "name": f"Out {n} ← In {m} level",
                    "uniq_id": f"dsp408_{self.slug}_out{n}_in{m}_level",
                    "stat_t": self.t(f"route/out{n}_in{m}/level/state"),
                    "cmd_t": self.t(f"route/out{n}_in{m}/level/set"),
                    "min": 0,
                    "max": 255,
                    "step": 1,
                    "icon": "mdi:tune-variant",
                    "mode": "box",
                    "ent_cat": "config",
                }
                for n in range(1, 9)
                for m in range(1, 5)
            },
            # ── Read-only per-channel state from the 296-byte blob ──
            # One sensor per channel; the JSON state is exposed via
            # json_attributes_topic so HA users can reference fields like
            # `state_attr('sensor.dsp408_<id>_ch1_state', 'hpf')` directly.
            # Main value shown in HA = the speaker-role name (e.g. "fl_high")
            # so the entity is human-glanceable.
            **{
                f"ch{n}_state": {
                    "p": "sensor",
                    "name": f"Channel {n} state",
                    "uniq_id": f"dsp408_{self.slug}_ch{n}_state",
                    "stat_t": self.t(f"ch{n}_state/state"),
                    "val_tpl": "{{ value_json.spk_type }}",
                    "json_attr_t": self.t(f"ch{n}_state/state"),
                    "ent_cat": "diagnostic",
                    "icon": "mdi:waveform",
                }
                for n in range(1, 9)
            },
        }
        return {
            "dev": dev,
            "o": {"name": "dsp408", "sw": "0.1.0",
                  "url": "https://github.com/malaiwah/dsp408-py"},
            "avty": avty,
            "avty_mode": "all",
            "cmps": cmps,
            "qos": 0,
        }

    # ── publishing helpers ──────────────────────────────────────────
    def publish(self, suffix: str, value, retain: bool = False,
                qos: int = 0) -> None:
        if isinstance(value, (dict, list)):
            payload: str | bytes = json.dumps(value)
        elif isinstance(value, bytes):
            payload = value.hex(" ")
        else:
            payload = str(value)
        self._client.publish(self.t(suffix), payload, qos=qos, retain=retain)

    def publish_availability(self, online: bool) -> None:
        # Only publish when the flag actually changes — this avoids
        # flicker in HA's log when poll-retries toggle state rapidly.
        if online == self._online_flag:
            return
        self._online_flag = online
        self._client.publish(
            self.availability_topic(),
            "online" if online else "offline",
            qos=1,
            retain=True,
        )

    def publish_discovery(self) -> None:
        """Publish the retained HA discovery document (QoS 1 so the broker
        reliably stores it across reconnects)."""
        doc = self.build_discovery_payload()
        self._client.publish(
            self.discovery_topic(),
            json.dumps(doc),
            qos=1,
            retain=True,
        )

    def clear_discovery(self) -> None:
        """Retract HA discovery so a device that's been unplugged
        permanently is removed from HA. Not called on transient
        disappearance (we leave the device registered but unavailable)."""
        self._client.publish(self.discovery_topic(), "", qos=1, retain=True)

    # ── inbound command dispatch ────────────────────────────────────
    def subscribe_commands(self) -> list[str]:
        """Return the list of topics this worker wants to subscribe to."""
        topics = [
            self.t("preset/set"),
            self.t("raw/read"),
            self.t("raw/write"),
            self.t("master_volume/set"),
            self.t("master_mute/set"),
        ]
        for n in range(1, 9):
            topics.append(self.t(f"ch{n}_volume/set"))
            topics.append(self.t(f"ch{n}_mute/set"))
            topics.append(self.t(f"ch{n}_polar/set"))
            topics.append(self.t(f"ch{n}_delay/set"))
        for n in range(1, 9):
            for m in range(1, 5):
                topics.append(self.t(f"route/out{n}_in{m}/set"))
                topics.append(self.t(f"route/out{n}_in{m}/level/set"))
        # EXPERIMENTAL system-register buttons
        topics.append(self.t("system/factory_reset/press"))
        for n in range(1, 7):
            topics.append(self.t(f"system/load_preset/{n}/press"))
        return topics

    # In-memory routing matrix mirror (no working device readback for empty
    # rows). Stored as u8 levels (0..255); 0 = OFF, anything else = ON.
    # Initialised from defaults; updated by every publish-write of a route.
    def _routing_state_init(self) -> None:
        if not hasattr(self, "_routing_mirror"):
            # 8 outputs × 4 inputs. Default OFF (level 0). Toggling a switch
            # ON writes 0x64 (unity) by convention; explicit level writes
            # bypass that and store the user-chosen value.
            self._routing_mirror = [[0] * 4 for _ in range(8)]

    def handle_command(self, topic: str, payload_bytes: bytes) -> None:
        """Dispatch an inbound MQTT command to the Device."""
        try:
            text = payload_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        log.debug("cmd %s → %s", topic, text[:80])
        try:
            if topic == self.t("preset/set"):
                self._ensure_device().write_preset_name(text.strip())
                # Re-publish to reflect the new state immediately
                self.publish("preset/state", text.strip(), retain=True, qos=1)
            elif topic == self.t("raw/read"):
                self._handle_raw(text, reply_suffix="raw/read/reply",
                                 is_write=False)
            elif topic == self.t("raw/write"):
                self._handle_raw(text, reply_suffix="raw/write/ack",
                                 is_write=True)
            elif topic == self.t("master_volume/set"):
                self._handle_master_volume(text)
            elif topic == self.t("master_mute/set"):
                self._handle_master_mute(text)
            elif topic.startswith(self.t("ch")) and topic.endswith("_volume/set"):
                self._handle_ch_volume(topic, text)
            elif topic.startswith(self.t("ch")) and topic.endswith("_mute/set"):
                self._handle_ch_mute(topic, text)
            elif topic.startswith(self.t("ch")) and topic.endswith("_polar/set"):
                self._handle_ch_polar(topic, text)
            elif topic.startswith(self.t("ch")) and topic.endswith("_delay/set"):
                self._handle_ch_delay(topic, text)
            # NOTE: order matters — check the longer "/level/set" suffix
            # *before* the catch-all bool route handler, otherwise a level
            # write would be parsed as a bool toggle.
            elif (topic.startswith(self.t("route/"))
                  and topic.endswith("/level/set")):
                self._handle_route_level(topic, text)
            elif topic.startswith(self.t("route/")) and topic.endswith("/set"):
                self._handle_route(topic, text)
            elif topic == self.t("system/factory_reset/press"):
                self._handle_factory_reset()
            elif (topic.startswith(self.t("system/load_preset/"))
                  and topic.endswith("/press")):
                self._handle_load_preset(topic)
            else:
                log.warning("unhandled topic %s", topic)
        except (DeviceNotFound, ProtocolError, OSError, ValueError) as e:
            log.error("command %s failed: %s", topic, e)
            self._close_device()
            self.publish("error/last", f"{topic}: {e}", retain=False)

    # ── per-control handlers ─────────────────────────────────────────
    def _handle_master_volume(self, text: str) -> None:
        db = float(text.strip())
        dev = self._ensure_device()
        dev.set_master_volume(db)
        self.publish("master_volume/state", f"{db:g}", retain=True, qos=1)

    def _handle_master_mute(self, text: str) -> None:
        muted = text.strip().upper() in ("ON", "TRUE", "1", "YES")
        dev = self._ensure_device()
        dev.set_master_mute(muted)
        self.publish("master_mute/state", "ON" if muted else "OFF",
                     retain=True, qos=1)

    def _handle_ch_volume(self, topic: str, text: str) -> None:
        # topic = "<base>/ch<N>_volume/set"; extract N
        n = int(topic.rsplit("/ch", 1)[1].split("_")[0])
        if not 1 <= n <= 8:
            raise ValueError(f"channel {n} out of range")
        db = float(text.strip())
        dev = self._ensure_device()
        dev.set_channel_volume(n - 1, db)
        self.publish(f"ch{n}_volume/state", f"{db:g}", retain=True, qos=1)

    def _handle_ch_mute(self, topic: str, text: str) -> None:
        n = int(topic.rsplit("/ch", 1)[1].split("_")[0])
        if not 1 <= n <= 8:
            raise ValueError(f"channel {n} out of range")
        muted = text.strip().upper() in ("ON", "TRUE", "1", "YES")
        dev = self._ensure_device()
        dev.set_channel_mute(n - 1, muted)
        self.publish(f"ch{n}_mute/state", "ON" if muted else "OFF",
                     retain=True, qos=1)

    # Default level written when a routing-bool switch is toggled ON.
    # 0x64 = decimal 100 = unity gain (matches what the legacy bool API
    # has always written). User-chosen levels (via the level slider) are
    # preserved across bool toggles unless the user toggles OFF first.
    _ROUTING_DEFAULT_ON_LEVEL = 0x64

    def _parse_route_topic(self, topic: str) -> tuple[int, int]:
        """Extract (out_n, in_m) — both 1-based — from a routing topic."""
        tail = topic.split("/route/", 1)[1]
        cell = tail.split("/", 1)[0]            # "out3_in2"
        out_n = int(cell.split("_")[0][3:])     # "out3" → 3
        in_m = int(cell.split("_")[1][2:])      # "in2"  → 2
        return out_n, in_m

    def _publish_routing_cell(self, out_n: int, in_m: int, level: int) -> None:
        """Publish both the bool and level state topics for one cell."""
        self.publish(f"route/out{out_n}_in{in_m}/state",
                     "ON" if level > 0 else "OFF", retain=True, qos=1)
        self.publish(f"route/out{out_n}_in{in_m}/level/state",
                     str(level), retain=True, qos=1)

    def _write_routing_row(self, out_idx: int) -> None:
        """Push the current mirror row to the device via set_routing_levels."""
        row = self._routing_mirror[out_idx]
        self._ensure_device().set_routing_levels(out_idx, row)

    def _handle_route(self, topic: str, text: str) -> None:
        out_n, in_m = self._parse_route_topic(topic)
        on = text.strip().upper() in ("ON", "TRUE", "1", "YES")
        self._routing_state_init()
        prev = self._routing_mirror[out_n - 1][in_m - 1]
        if on:
            # Preserve any user-chosen non-zero level; only seed unity if
            # the cell was previously OFF.
            level = prev if prev > 0 else self._ROUTING_DEFAULT_ON_LEVEL
        else:
            level = 0
        self._routing_mirror[out_n - 1][in_m - 1] = level
        self._write_routing_row(out_n - 1)
        self._publish_routing_cell(out_n, in_m, level)

    def _handle_route_level(self, topic: str, text: str) -> None:
        # topic = "<base>/route/out<N>_in<M>/level/set"
        out_n, in_m = self._parse_route_topic(topic)
        try:
            level = int(float(text.strip()))
        except ValueError as e:
            raise ValueError(f"route level must be 0..255, got {text!r}") from e
        if not 0 <= level <= 255:
            raise ValueError(f"route level out of range: {level}")
        self._routing_state_init()
        self._routing_mirror[out_n - 1][in_m - 1] = level
        self._write_routing_row(out_n - 1)
        self._publish_routing_cell(out_n, in_m, level)

    def _handle_ch_polar(self, topic: str, text: str) -> None:
        n = int(topic.rsplit("/ch", 1)[1].split("_")[0])
        if not 1 <= n <= 8:
            raise ValueError(f"channel {n} out of range")
        polar = text.strip().upper() in ("ON", "TRUE", "1", "YES")
        self._ensure_device().set_channel_polar(n - 1, polar)
        self.publish(f"ch{n}_polar/state",
                     "ON" if polar else "OFF", retain=True, qos=1)

    def _handle_ch_delay(self, topic: str, text: str) -> None:
        n = int(topic.rsplit("/ch", 1)[1].split("_")[0])
        if not 1 <= n <= 8:
            raise ValueError(f"channel {n} out of range")
        try:
            samples = int(float(text.strip()))
        except ValueError as e:
            raise ValueError(f"delay must be an integer, got {text!r}") from e
        if not 0 <= samples <= 0xFFFF:
            raise ValueError(f"delay out of u16 range: {samples}")
        # Preserve volume + mute from the last cached set; the firmware
        # silently caps anything above 359 taps.
        dev = self._ensure_device()
        cached = dev.get_channel_cached(n - 1)
        dev.set_channel(n - 1, db=cached["db"], muted=cached["muted"],
                        delay_samples=samples)
        self.publish(f"ch{n}_delay/state", str(samples), retain=True, qos=1)

    def _handle_factory_reset(self) -> None:
        """EXPERIMENTAL: trigger the magic-word factory-reset register write.

        Logged loudly because it's destructive — wipes any stored config.
        Wire encoding has not been live-validated, so this might silently
        no-op on a real device. See dsp408.Device.factory_reset() docstring.
        """
        log.warning("%s: factory_reset button pressed — issuing magic 0xA5A6 "
                    "to register 0x061F", self.slug)
        self._ensure_device().factory_reset()
        self.publish("system/factory_reset/last",
                     "issued", retain=False)

    def _handle_load_preset(self, topic: str) -> None:
        """EXPERIMENTAL: load one of the 6 built-in factory presets.

        topic = "<base>/system/load_preset/<N>/press"
        """
        # extract N from ".../load_preset/<N>/press"
        try:
            n_str = topic.split("/load_preset/", 1)[1].split("/", 1)[0]
            n = int(n_str)
        except (IndexError, ValueError) as e:
            raise ValueError(f"can't parse preset id from {topic!r}") from e
        log.warning("%s: load_factory_preset(%d) — issuing magic 0x%04x",
                    self.slug, n, 0xB500 | n)
        self._ensure_device().load_factory_preset(n)
        self.publish(f"system/load_preset/{n}/last",
                     "issued", retain=False)

    def _handle_raw(self, text: str, reply_suffix: str, is_write: bool) -> None:
        try:
            req = json.loads(text) if text else {}
        except json.JSONDecodeError as e:
            self.publish(reply_suffix, {"error": f"bad json: {e}"})
            return
        if "cmd" not in req:
            self.publish(reply_suffix, {"error": "missing 'cmd' key"})
            return
        try:
            cmd_val = req["cmd"]
            cmd = int(cmd_val, 16) if isinstance(cmd_val, str) else int(cmd_val)
            cat = req.get("cat")
            if isinstance(cat, str):
                cat = int(cat, 16)
            if cat is None:
                cat = category_hint(cmd)
        except (ValueError, TypeError) as e:
            self.publish(reply_suffix, {"error": f"bad request: {e}"})
            return
        dev = self._ensure_device()
        if is_write:
            data_hex = (req.get("data_hex") or "").replace(" ", "")
            data = bytes.fromhex(data_hex)
            reply = dev.write_raw(cmd=cmd, data=data, category=cat)
        else:
            reply = dev.read_raw(cmd=cmd, category=cat)
        self.publish(reply_suffix, {
            "cmd": f"0x{reply.cmd:04x}",
            "cat": f"0x{reply.category:02x}",
            "dir": f"0x{reply.direction:02x}",
            "seq": reply.seq,
            "len": reply.payload_len,
            "chk_ok": reply.checksum_ok,
            "payload_hex": reply.payload.hex(),
        })

    # ── poll loop ───────────────────────────────────────────────────
    def _poll_once(self) -> None:
        dev = self._ensure_device()
        identity = dev.get_info()
        if identity != self._identity_cached:
            self._identity_cached = identity
            # Re-publish discovery so `sw` field updates in HA
            self.publish_discovery()
        preset = dev.read_preset_name()
        status_byte = dev.read_status()
        state13 = dev.read_state_0x13()
        _, _, g06 = dev.read_globals()
        master_db, master_muted = dev.get_master()

        self.publish("identity/state", identity, retain=True)
        self.publish("preset/state", preset, retain=True)
        self.publish("status_byte/state", status_byte, retain=True)
        self.publish("state_0x13/state", state13, retain=False)
        self.publish("global_06/state", g06, retain=False)
        # Master is the only sliders that has a reliable readback. Per-
        # channel volume + mute and routing reads return EQ-table data,
        # so for those we publish what we last set (cached) — the user's
        # HA-side actions go through us, so the cache stays consistent.
        self.publish("master_volume/state", f"{master_db:g}", retain=True)
        self.publish("master_mute/state",
                     "ON" if master_muted else "OFF", retain=True)

    def _publish_channel_state(self, n: int, state: dict) -> None:
        """Publish the per-channel JSON state document.

        Picks the human-friendly subset of get_channel()'s return dict and
        publishes it as a single retained MQTT message at
        ``ch{n}_state/state``.  Drops the ``raw`` blob bytes (not useful in
        HA) and uses the ``protocol`` enum maps to decode filter type +
        slope into human strings.

        HA sees this as one diagnostic sensor per channel; users can
        extract specific fields with template sensors / value_template.
        """
        from .protocol import (
            FILTER_TYPE_NAMES,
            SLOPE_NAMES,
            SPK_TYPE_NAMES,
        )

        def _filter_name(idx: int) -> str:
            return (FILTER_TYPE_NAMES[idx]
                    if 0 <= idx < len(FILTER_TYPE_NAMES) else f"unknown({idx})")

        def _slope_name(idx: int) -> str:
            return (SLOPE_NAMES[idx]
                    if 0 <= idx < len(SLOPE_NAMES) else f"unknown({idx})")

        def _spk_name(idx: int) -> str:
            return (SPK_TYPE_NAMES[idx]
                    if 0 <= idx < len(SPK_TYPE_NAMES) else f"custom({idx:#04x})")

        hpf = state.get("hpf") or {}
        lpf = state.get("lpf") or {}
        comp = state.get("compressor") or {}
        delay_samples = int(state.get("delay", 0))
        doc = {
            "polar": bool(state.get("polar", False)),
            "eq_mode": int(state.get("eq_mode", 0)),
            "spk_type": _spk_name(int(state.get("spk_type", 0))),
            "spk_type_raw": int(state.get("spk_type", 0)),
            "name": state.get("name", ""),
            "linkgroup": int(state.get("linkgroup", 0)),
            # delay in both raw samples and milliseconds @ 48 kHz (the
            # firmware encoding is taps; ms is provided for human convenience)
            "delay_samples": delay_samples,
            "delay_ms": round(delay_samples * 1000.0 / 48000, 4),
            "hpf": {
                "freq_hz": int(hpf.get("freq", 0)),
                "filter": _filter_name(int(hpf.get("filter", 0))),
                "slope": _slope_name(int(hpf.get("slope", 0))),
            },
            "lpf": {
                "freq_hz": int(lpf.get("freq", 0)),
                "filter": _filter_name(int(lpf.get("filter", 0))),
                "slope": _slope_name(int(lpf.get("slope", 0))),
            },
            "mixer": list(state.get("mixer", [])),
            "compressor": {
                "attack_ms": int(comp.get("attack_ms", 0)),
                "release_ms": int(comp.get("release_ms", 0)),
                "threshold": int(comp.get("threshold", 0)),
                "all_pass_q": int(comp.get("all_pass_q", 0)),
            },
        }
        self.publish(f"ch{n}_state", doc, retain=True, qos=1)

    def publish_initial_cached_state(self) -> None:
        """Read device state and publish it as initial MQTT retained values.

        Calls ``get_channel()`` for each of the 8 outputs, which issues a
        real ``cmd=0x77NN`` USB read and parses the 296-byte blob.  This
        replaces the old approach of publishing defaults from an
        in-memory cache, so HA sees the actual device state even after a
        bridge restart.

        Routing caveats: the blob parser can only detect a routing row as
        ON when at least one input is ON (a non-zero level).  If all
        inputs for an output are OFF the pattern is ambiguous and we fall
        back to whatever the routing mirror already holds (typically all
        OFF, which is the device's power-on default).

        Per-channel routing state read from the blob is merged into the
        worker's ``_routing_mirror`` so subsequent toggle commands work
        from the correct baseline.
        """
        self._routing_state_init()
        try:
            dev = self._ensure_device()
        except Exception as e:
            log.warning("%s: initial state read skipped (no device): %s",
                        self.slug, e)
            return

        for n in range(1, 9):
            channel = n - 1
            try:
                state = dev.get_channel(channel)
            except Exception as e:
                log.warning(
                    "%s: ch%d state read failed, using cached defaults: %s",
                    self.slug, n, e,
                )
                # Fall back to whatever is in the cache (default: 0 dB, unmuted)
                cached = dev.get_channel_cached(channel)
                self.publish(f"ch{n}_volume/state",
                             f"{cached['db']:g}", retain=True, qos=1)
                self.publish(f"ch{n}_mute/state",
                             "ON" if cached["muted"] else "OFF",
                             retain=True, qos=1)
                self.publish(f"ch{n}_polar/state",
                             "ON" if cached.get("polar") else "OFF",
                             retain=True, qos=1)
                self.publish(f"ch{n}_delay/state",
                             str(int(cached.get("delay", 0))),
                             retain=True, qos=1)
                continue

            # Successfully read from device — publish actual state.
            self.publish(f"ch{n}_volume/state",
                         f"{state['db']:g}", retain=True, qos=1)
            self.publish(f"ch{n}_mute/state",
                         "ON" if state["muted"] else "OFF",
                         retain=True, qos=1)
            self.publish(f"ch{n}_polar/state",
                         "ON" if state.get("polar") else "OFF",
                         retain=True, qos=1)
            self.publish(f"ch{n}_delay/state",
                         str(int(state.get("delay", 0))),
                         retain=True, qos=1)

            # Publish full per-channel state as a single JSON sensor — exposes
            # phase, crossover, mixer, compressor, link group, and channel name
            # in one message rather than 25+ individual entities. Users can
            # extract specific fields via HA template sensors as needed.
            self._publish_channel_state(n, state)

            # The blob's mixer field IS the routing row for this output
            # (cells [0..3] = IN1..IN4 levels). Read it and seed the
            # mirror so subsequent toggles build on the device's actual
            # baseline. We accept all-zero rows too — a fully-OFF row is
            # legitimate state, not "missing data".
            mixer = state.get("mixer") or []
            if len(mixer) >= 4:
                row = [int(mixer[i]) & 0xFF for i in range(4)]
                self._routing_mirror[channel] = row
                log.debug("%s: ch%d routing levels from device: %s",
                          self.slug, n, row)

        # Publish the routing matrix state (from mirror, just refreshed
        # above from the device blob). Each cell publishes BOTH the bool
        # state (ON/OFF) and the numeric level (0..255).
        for n in range(1, 9):
            for m in range(1, 5):
                level = self._routing_mirror[n - 1][m - 1]
                self._publish_routing_cell(n, m, level)

    def run(self) -> None:
        """Thread entry point. Polls the device until stopped.

        Announces discovery (with best-effort initial identity), then
        loops: poll → sleep. On error, close + exponential backoff +
        flip availability offline. We only flip availability back to
        online on a *successful* poll (not merely on re-open), which
        avoids flicker when the USB handle opens but the first read
        then times out.
        """
        try:
            # Read identity once synchronously so discovery has the right sw
            dev = self._ensure_device()
            self._identity_cached = dev.get_info()
        except Exception as e:
            log.warning("%s: initial get_info failed: %s", self.slug, e)
        self.publish_discovery()
        # HA needs initial values for sliders/switches to render properly,
        # so publish per-channel + routing defaults from cache. Master is
        # populated by the regular poll loop.
        self.publish_initial_cached_state()

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._poll_once()
                # Poll succeeded — announce online (no-op if already online)
                self.publish_availability(True)
                backoff = 1.0
            except (DeviceNotFound, ProtocolError, OSError) as e:
                log.warning("%s: poll failed: %s (retry in %.1fs)",
                            self.slug, e, backoff)
                self._close_device()
                self.publish_availability(False)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self._stop.wait(self._cfg.poll_interval)

        self.publish_availability(False)
        self._close_device()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.run,
            name=f"dsp408-worker-{self.slug}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


# ── Top-level bridge ───────────────────────────────────────────────────
class MqttBridge:
    """MQTT bridge that manages one DeviceWorker per connected DSP-408.

    Thread model:
        * main thread: run() — hotplug loop that spawns/reaps workers.
        * paho network thread: on_message / on_connect callbacks.
        * N worker threads: one per DSP-408, polling state.
    All three touch `self._workers`, so access is guarded by
    `self._workers_lock`.

    Usage:

        bridge = MqttBridge(broker="mqtt.local", username=..., password=...)
        bridge.run()   # blocks; SIGINT to stop

    Or with a dedicated thread:

        bridge.start()
        ... your code ...
        bridge.stop()
    """

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        base_topic: str = "dsp408",
        discovery_prefix: str = "homeassistant",
        poll_interval: float = 2.0,
        selector: str | None = None,
        aliases: dict[str, str] | None = None,
    ):
        self.cfg = BridgeConfig(
            broker=broker,
            port=port,
            username=username,
            password=password,
            base_topic=base_topic,
            discovery_prefix=discovery_prefix,
            poll_interval=poll_interval,
            selector=selector,
            aliases=aliases or {},
        )
        self._workers: dict[str, DeviceWorker] = {}
        self._workers_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Use MQTTv5 callback API if available, else fall back
        self._client, self._paho_v2 = _make_mqtt_client()
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ── topics → worker routing ─────────────────────────────────────
    def _worker_for_topic(self, topic: str) -> DeviceWorker | None:
        prefix = f"{self.cfg.base_topic}/"
        if not topic.startswith(prefix):
            return None
        slug = topic[len(prefix):].split("/", 1)[0]
        with self._workers_lock:
            return self._workers.get(slug)

    def _workers_snapshot(self) -> list[DeviceWorker]:
        """Thread-safe immutable snapshot for iteration."""
        with self._workers_lock:
            return list(self._workers.values())

    # ── paho callbacks ──────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """paho v1 signature: (client, userdata, flags, rc).
        paho v2 signature: (client, userdata, flags, reason_code, properties).
        `properties` has a default so both work.

        `rc` is an int in v1, a `ReasonCode` in v2 (which doesn't implement
        `__int__` but exposes `.value` and `.is_failure`).
        """
        ok = _rc_is_success(rc)
        if not ok:
            log.error("MQTT connect failed: rc=%s", rc)
            return
        log.info("MQTT connected to %s:%d", self.cfg.broker, self.cfg.port)
        # Publish bridge-level "online" (LWT will flip this to offline
        # automatically if we die; also published explicitly on stop()).
        self._client.publish(
            bridge_status_topic(self.cfg.base_topic),
            "online",
            qos=1,
            retain=True,
        )
        # Re-subscribe for every worker (restore after reconnect)
        for w in self._workers_snapshot():
            for t in w.subscribe_commands():
                self._client.subscribe(t, qos=0)
            w.publish_discovery()

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        log.warning("MQTT disconnected")

    def _on_message(self, client, userdata, msg):
        w = self._worker_for_topic(msg.topic)
        if w is None:
            log.debug("unmatched topic %s", msg.topic)
            return
        w.handle_command(msg.topic, msg.payload)

    # ── lifecycle ───────────────────────────────────────────────────
    def _select_devices(self) -> list[dict]:
        all_devs = enumerate_devices(aliases=self.cfg.aliases)
        if self.cfg.selector is None:
            return all_devs
        try:
            return [resolve_selector(self.cfg.selector, all_devs)]
        except DeviceNotFound:
            return []

    def _initial_spawn(self) -> None:
        """Create worker objects for each device found at startup (but do
        NOT start their threads yet — that happens after MQTT connects)."""
        devs = self._select_devices()
        if not devs:
            log.warning("no DSP-408 found on USB — bridge will idle until one is plugged in")
        with self._workers_lock:
            for d in devs:
                slug = sanitize_id(d["display_id"])
                if slug in self._workers:
                    continue
                self._workers[slug] = DeviceWorker(self._client, d, self.cfg)

    def run(self) -> None:
        """Blocking run: connect to MQTT, spawn workers, loop forever."""
        self._initial_spawn()

        # Bridge-level LWT → "offline" on unclean disconnect. This is the
        # ONE will supported by paho; combined with the per-device
        # availability topic (which workers explicitly set offline when
        # they fail to poll), a crashed bridge correctly flips every
        # device to unavailable in HA.
        self._client.will_set(
            bridge_status_topic(self.cfg.base_topic),
            "offline",
            qos=1,
            retain=True,
        )
        self._client.connect(self.cfg.broker, self.cfg.port, keepalive=30)
        self._client.loop_start()

        try:
            for w in self._workers_snapshot():
                w.start()
            log.info("bridge running for %d device(s): %s",
                     len(self._workers),
                     [w.slug for w in self._workers_snapshot()])
            while not self._stop.is_set():
                self._stop.wait(1.0)
                self._hotplug_sync()
        finally:
            self.stop()

    def _hotplug_sync(self) -> None:
        """Reap workers whose device disappeared; spawn new ones for
        newly-plugged devices."""
        new_devs = self._select_devices()
        new_slugs = {sanitize_id(d["display_id"]) for d in new_devs}

        # Compute diff under lock to avoid racing with paho callbacks
        to_reap: list[DeviceWorker] = []
        to_add: list[dict] = []
        with self._workers_lock:
            for slug in list(self._workers.keys()):
                if slug not in new_slugs:
                    to_reap.append(self._workers.pop(slug))
            existing = set(self._workers.keys())
            for d in new_devs:
                slug = sanitize_id(d["display_id"])
                if slug not in existing:
                    w = DeviceWorker(self._client, d, self.cfg)
                    self._workers[slug] = w
                    to_add.append(d)

        # Side effects (publish/subscribe/thread start/stop) OUTSIDE
        # the lock to avoid holding it during network I/O.
        for w in to_reap:
            log.info("device disappeared: %s", w.slug)
            w.publish_availability(False)
            w.stop()
        for d in to_add:
            slug = sanitize_id(d["display_id"])
            log.info("device appeared: %s", slug)
            # Grab the worker back under lock (it's already in the dict)
            with self._workers_lock:
                w = self._workers.get(slug)
            if w is None:
                continue
            for t in w.subscribe_commands():
                self._client.subscribe(t, qos=0)
            w.start()

    def start(self) -> None:
        """Start the bridge in a background thread (non-blocking)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="dsp408-mqtt-bridge",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        log.info("stopping bridge...")
        # Publish bridge-level offline BEFORE disconnecting so HA gets
        # the clean signal (LWT would also do this, but explicit is
        # better for planned shutdown).
        try:
            self._client.publish(
                bridge_status_topic(self.cfg.base_topic),
                "offline",
                qos=1,
                retain=True,
            )
        except Exception:
            pass
        for w in self._workers_snapshot():
            try:
                w.publish_availability(False)
            except Exception:
                pass
            w.stop()
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


def _make_mqtt_client() -> tuple[mqtt.Client, bool]:
    """Create a paho-mqtt Client compatible with v1.x and v2.x.

    paho-mqtt 2.0 moved to a new callback API; passing
    `callback_api_version=CallbackAPIVersion.VERSION2` avoids a
    DeprecationWarning and future breakage.

    Returns (client, is_v2_callback_api).
    """
    try:
        v2 = mqtt.CallbackAPIVersion.VERSION2  # type: ignore[attr-defined]
        return mqtt.Client(callback_api_version=v2), True
    except AttributeError:
        # paho-mqtt < 2.0 doesn't have CallbackAPIVersion
        return mqtt.Client(), False


__all__ = [
    "MqttBridge",
    "DeviceWorker",
    "BridgeConfig",
    "sanitize_id",
    "bridge_status_topic",
]
