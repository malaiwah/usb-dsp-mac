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

        dev = {
            # HA abbreviations:
            "ids": [f"dsp408_{self.slug}"],
            "name": f"DSP-408 ({self._info['display_id']})",
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
        }
        return {
            "dev": dev,
            "o": {"name": "dsp408", "sw": "0.1.0",
                  "url": "https://github.com/mbelleau/usb_dsp_mac"},
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
        return [
            self.t("preset/set"),
            self.t("raw/read"),
            self.t("raw/write"),
        ]

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
            else:
                log.warning("unhandled topic %s", topic)
        except (DeviceNotFound, ProtocolError, OSError) as e:
            log.error("command %s failed: %s", topic, e)
            self._close_device()
            self.publish("error/last", f"{topic}: {e}", retain=False)

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

        self.publish("identity/state", identity, retain=True)
        self.publish("preset/state", preset, retain=True)
        self.publish("status_byte/state", status_byte, retain=True)
        self.publish("state_0x13/state", state13, retain=False)
        self.publish("global_06/state", g06, retain=False)

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
        `properties` has a default so both work."""
        ok = (int(rc) == 0)
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
        all_devs = enumerate_devices()
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
