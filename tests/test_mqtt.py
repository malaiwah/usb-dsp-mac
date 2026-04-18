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
        # can reflect external state changes.
        if "cmd_t" in cmp:
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
