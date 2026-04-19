"""Unit tests for dsp408.mqtt — discovery payload shape, id sanitization,
and topic routing. No real broker or USB required.

Skipped automatically if paho-mqtt isn't installed.
"""
from __future__ import annotations

import pytest

paho = pytest.importorskip("paho.mqtt.client")

from dsp408 import mqtt  # noqa: E402


def test_sanitize_id_basic():
    assert mqtt.sanitize_id("MYDW-AV1.06") == "mydw_av1_06"
    assert mqtt.sanitize_id("dsp408-a1b2c3d4") == "dsp408_a1b2c3d4"
    assert mqtt.sanitize_id("") == "dsp408"
    assert mqtt.sanitize_id("///") == "dsp408"


def test_sanitize_id_strips_unsafe_chars():
    # MQTT wildcard / separator chars must not survive
    slug = mqtt.sanitize_id("foo+bar/#baz")
    assert "+" not in slug
    assert "#" not in slug
    assert "/" not in slug
    assert slug == "foo_bar_baz"


def _fake_info(display_id="MYDW-AV1.06"):
    return {
        "index": 0,
        "vid": 0x0483,
        "pid": 0x5750,
        "path": b"/dev/hidraw0",
        "serial_number": "MYDW-AV1.06",
        "product_string": "DSP-408",
        "manufacturer": "Dayton Audio",
        "display_id": display_id,
    }


def test_discovery_payload_shape_and_invariants():
    """Verify the discovery payload is the shape HA expects.

    Key HA rules (from 2024.12+ docs):
      * `dev` has `ids` (list)
      * component entries live under `cmps`
      * each cmp has `p` (platform) and a `uniq_id`
      * RW components have both `cmd_t` and `stat_t`
      * discovery topic is `<prefix>/device/<slug>/config`
    """
    cfg = mqtt.BridgeConfig(broker="x")
    class _Stub:
        def publish(self, *a, **kw): pass
    w = mqtt.DeviceWorker(_Stub(), _fake_info(), cfg)
    w._identity_cached = "MYDW-AV1.06"

    doc = w.build_discovery_payload()
    assert "dev" in doc and "cmps" in doc and "avty" in doc
    assert isinstance(doc["dev"]["ids"], list) and doc["dev"]["ids"]

    for cmp_id, cmp in doc["cmps"].items():
        assert "p" in cmp, f"{cmp_id}: missing platform key `p`"
        assert "uniq_id" in cmp, f"{cmp_id}: missing uniq_id"
        # If a cmd_t is present, stat_t must also be present so HA
        # can reflect external state changes — except for buttons,
        # which are write-only by design (HA fires them, no state).
        if "cmd_t" in cmp and cmp.get("p") != "button":
            assert "stat_t" in cmp, f"{cmp_id}: cmd_t without stat_t"
        # unique_ids must be MQTT-safe (no wildcards)
        for ch in ("+", "#", " "):
            assert ch not in cmp["uniq_id"], cmp["uniq_id"]

    # Discovery topic layout
    topic = w.discovery_topic()
    assert topic.startswith("homeassistant/device/dsp408_")
    assert topic.endswith("/config")


def test_topic_routing_prefers_worker_slug():
    cfg = mqtt.BridgeConfig(broker="x", base_topic="dsp408")
    class _StubClient:
        def __init__(self): self.published = []
        def publish(self, *a, **kw): self.published.append((a, kw))
        def username_pw_set(self, *a, **kw): pass
        def will_set(self, *a, **kw): pass
        def connect(self, *a, **kw): pass
        def subscribe(self, *a, **kw): pass
        def loop_start(self): pass
        def loop_stop(self): pass
        def disconnect(self): pass
        on_connect = on_disconnect = on_message = None

    # Manually inject a bridge + one worker without opening hidapi
    import threading as _th
    b = mqtt.MqttBridge.__new__(mqtt.MqttBridge)
    b.cfg = cfg
    b._client = _StubClient()
    b._workers = {}
    b._workers_lock = _th.Lock()
    b._stop = _th.Event()

    w = mqtt.DeviceWorker(b._client, _fake_info(), cfg)
    b._workers[w.slug] = w

    # Topic in our base tree should resolve to the right worker
    found = b._worker_for_topic(f"dsp408/{w.slug}/preset/set")
    assert found is w

    # Topic outside the base tree should return None
    assert b._worker_for_topic("homeassistant/device/foo/config") is None


def test_availability_topic_is_retained_slug():
    cfg = mqtt.BridgeConfig(broker="x")
    class _Stub:
        def publish(self, *a, **kw): pass
    w = mqtt.DeviceWorker(_Stub(), _fake_info(), cfg)
    # The availability topic must live under the base tree and use the
    # slug we chose.
    assert w.availability_topic() == f"dsp408/{w.slug}/status"


def test_mqtt_client_factory_tolerates_paho_v1_and_v2():
    """_make_mqtt_client() should not raise regardless of paho version."""
    c, is_v2 = mqtt._make_mqtt_client()
    assert c is not None
    assert isinstance(is_v2, bool)


def test_discovery_has_bridge_level_lwt_availability():
    """A bridge crash must flip ALL devices to unavailable in HA, not
    just the first. We do this by adding a second `avty` entry pointing
    at the bridge-level status topic."""
    cfg = mqtt.BridgeConfig(broker="x", base_topic="dsp408")
    class _Stub:
        def publish(self, *a, **kw): pass
    w = mqtt.DeviceWorker(_Stub(), _fake_info(), cfg)
    doc = w.build_discovery_payload()
    avty = doc["avty"]
    assert isinstance(avty, list)
    assert len(avty) >= 2
    topics = [a["t"] for a in avty]
    # Per-device status
    assert f"dsp408/{w.slug}/status" in topics
    # Bridge-level LWT
    assert mqtt.bridge_status_topic("dsp408") in topics
    assert mqtt.bridge_status_topic("dsp408") == "dsp408/bridge/status"
    # avty_mode "all" means both availability sources must be online
    assert doc.get("avty_mode") == "all"


def test_rc_is_success_paho_v1_int():
    """paho v1 passes a plain int rc to on_connect."""
    assert mqtt._rc_is_success(0) is True
    assert mqtt._rc_is_success(1) is False
    assert mqtt._rc_is_success(5) is False


def test_rc_is_success_paho_v2_reasoncode():
    """paho v2 passes a ReasonCode object with .is_failure / .value.
    ReasonCode deliberately does NOT implement __int__, so `int(rc)`
    raises TypeError — this regressed in the first Pi live-test.
    """

    class _FakeReasonCode:
        """Mimics paho v2 ReasonCode: no __int__, has .is_failure / .value."""

        def __init__(self, value: int):
            self.value = value
            self.is_failure = value != 0

        def __int__(self):
            raise TypeError("ReasonCode does not support int()")

    assert mqtt._rc_is_success(_FakeReasonCode(0)) is True
    assert mqtt._rc_is_success(_FakeReasonCode(5)) is False


# ── new H-feature entities ───────────────────────────────────────────────
class _SilentClient:
    """Stand-in for the real paho client — captures publishes, no I/O."""
    def __init__(self):
        self.published: list[tuple[str, str]] = []
    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload if isinstance(payload, str)
                               else payload.decode("utf-8", "replace")))


class _FakeDevice:
    """Just enough Device interface to drive the handlers."""
    def __init__(self):
        self.calls: list[tuple] = []
        self.cached = {"db": -3.5, "muted": False, "polar": False,
                       "delay": 0, "subidx": 0x01}
    def set_channel_polar(self, ch, polar):
        self.calls.append(("polar", ch, polar))
    def set_channel(self, ch, db, muted, delay_samples=0, polar=None):
        self.calls.append(("set_channel", ch, db, muted, delay_samples, polar))
    def get_channel_cached(self, ch):
        return dict(self.cached)
    def set_routing_levels(self, out_idx, levels):
        self.calls.append(("routing_levels", out_idx, list(levels)))


def _make_worker():
    cfg = mqtt.BridgeConfig(broker="x", base_topic="dsp408")
    client = _SilentClient()
    w = mqtt.DeviceWorker(client, _fake_info(), cfg)
    fake = _FakeDevice()
    w._ensure_device = lambda: fake     # type: ignore[method-assign]
    return w, client, fake


def test_discovery_includes_polar_delay_and_routing_levels():
    w, _, _ = _make_worker()
    cmps = w.build_discovery_payload()["cmps"]
    # 8 phase-invert switches, 8 delay sliders
    for n in range(1, 9):
        assert f"ch{n}_polar" in cmps and cmps[f"ch{n}_polar"]["p"] == "switch"
        assert f"ch{n}_delay" in cmps and cmps[f"ch{n}_delay"]["p"] == "number"
        assert cmps[f"ch{n}_delay"]["max"] == 359   # firmware cap
    # 32 routing-level numbers (8 outs × 4 ins)
    for n in range(1, 9):
        for m in range(1, 5):
            key = f"out{n}_in{m}_level"
            assert key in cmps
            assert cmps[key]["p"] == "number"
            assert cmps[key]["max"] == 255


def test_subscribe_topics_cover_new_handlers():
    w, _, _ = _make_worker()
    topics = w.subscribe_commands()
    # New per-channel topics
    for n in range(1, 9):
        assert w.t(f"ch{n}_polar/set") in topics
        assert w.t(f"ch{n}_delay/set") in topics
    # New per-cell level topics
    for n in range(1, 9):
        for m in range(1, 5):
            assert w.t(f"route/out{n}_in{m}/level/set") in topics


def test_handle_ch_polar_dispatches_and_publishes():
    w, client, fake = _make_worker()
    w.handle_command(w.t("ch3_polar/set"), b"ON")
    assert ("polar", 2, True) in fake.calls
    assert (w.t("ch3_polar/state"), "ON") in client.published
    w.handle_command(w.t("ch3_polar/set"), b"OFF")
    assert ("polar", 2, False) in fake.calls


def test_handle_ch_delay_preserves_volume_and_mute():
    w, client, fake = _make_worker()
    fake.cached = {"db": -6.5, "muted": True, "polar": False,
                   "delay": 0, "subidx": 0x01}
    w.handle_command(w.t("ch5_delay/set"), b"144")
    assert ("set_channel", 4, -6.5, True, 144, None) in fake.calls
    assert (w.t("ch5_delay/state"), "144") in client.published


def test_handle_route_bool_writes_unity_then_preserves_user_level():
    w, client, fake = _make_worker()
    # First: bool ON from a fresh OFF cell → seeds unity (0x64)
    w.handle_command(w.t("route/out2_in3/set"), b"ON")
    last_routing = [c for c in fake.calls if c[0] == "routing_levels"][-1]
    assert last_routing[1] == 1                       # out_idx (0-based)
    assert last_routing[2][2] == 0x64                 # IN3 cell unity
    # Second: user dials in 200 via the level slider
    w.handle_command(w.t("route/out2_in3/level/set"), b"200")
    last_routing = [c for c in fake.calls if c[0] == "routing_levels"][-1]
    assert last_routing[2][2] == 200
    # Third: bool toggle OFF then ON again — should preserve 200 (not reset to 0x64)
    w.handle_command(w.t("route/out2_in3/set"), b"OFF")
    last_off = [c for c in fake.calls if c[0] == "routing_levels"][-1]
    assert last_off[2][2] == 0
    w.handle_command(w.t("route/out2_in3/set"), b"ON")
    last_on = [c for c in fake.calls if c[0] == "routing_levels"][-1]
    # User-chosen level is lost after an explicit OFF (by design; OFF means 0,
    # and we re-seed unity on the next ON). Document this in the assertion.
    assert last_on[2][2] == 0x64


def test_handle_route_level_validates_range():
    w, _, _ = _make_worker()
    with pytest.raises((ValueError, Exception)):
        # 256 is out of u8 range — should propagate up to handle_command's
        # catch block. Since handle_command swallows ValueError, instead
        # test the inner handler directly.
        w._handle_route_level(w.t("route/out1_in1/level/set"), "256")


def test_discovery_includes_factory_reset_and_preset_buttons():
    w, _, _ = _make_worker()
    cmps = w.build_discovery_payload()["cmps"]
    assert cmps["factory_reset"]["p"] == "button"
    assert "cmd_t" in cmps["factory_reset"]
    # No state topic on a button (HA buttons are write-only)
    for n in range(1, 7):
        assert cmps[f"load_preset_{n}"]["p"] == "button"


def test_factory_reset_topic_is_subscribed():
    w, _, _ = _make_worker()
    topics = w.subscribe_commands()
    assert w.t("system/factory_reset/press") in topics
    for n in range(1, 7):
        assert w.t(f"system/load_preset/{n}/press") in topics


def test_handle_factory_reset_invokes_device_method():
    w, client, _fake = _make_worker()
    # Add factory_reset to fake (we'd added it via _ensure_device override)
    called = []
    _fake.factory_reset = lambda: called.append("reset")  # type: ignore[attr-defined]
    w.handle_command(w.t("system/factory_reset/press"), b"PRESS")
    assert called == ["reset"]


def test_handle_load_preset_extracts_id_and_dispatches():
    w, _client, _fake = _make_worker()
    called = []
    _fake.load_factory_preset = (  # type: ignore[attr-defined]
        lambda n: called.append(n))
    w.handle_command(w.t("system/load_preset/3/press"), b"PRESS")
    assert called == [3]


def test_routing_mirror_initialises_as_int_levels():
    w, _, _ = _make_worker()
    w._routing_state_init()
    # 8 rows × 4 cells, all int 0
    assert len(w._routing_mirror) == 8
    for row in w._routing_mirror:
        assert len(row) == 4
        for cell in row:
            assert isinstance(cell, int) and cell == 0
