"""Unit tests for the pure-python bits of multi-device enumeration.

Exercises the display_id + selector resolver logic without touching
hidapi or real USB. The idea is that if two DSP-408s are attached and
share the same serial number (common on cheap firmware), the
library must still give them distinct, stable identifiers.
"""
from __future__ import annotations

import pytest

from dsp408.device import (
    DeviceNotFound,
    _build_display_id,
    _resolve_selector,
)


def _enum_from_raw(raw: list[dict]) -> list[dict]:
    """Mimic what enumerate_devices() does to a list of hidapi dicts."""
    serial_counts: dict[str, int] = {}
    for d in raw:
        s = (d.get("serial_number") or "").strip()
        if s:
            serial_counts[s] = serial_counts.get(s, 0) + 1
    out = []
    for idx, d in enumerate(raw):
        out.append({
            "index": idx,
            "vid": d.get("vendor_id", 0x0483),
            "pid": d.get("product_id", 0x5750),
            "path": d.get("path") or b"",
            "serial_number": (d.get("serial_number") or "").strip(),
            "product_string": (d.get("product_string") or "").strip(),
            "manufacturer": (d.get("manufacturer_string") or "").strip(),
            "display_id": _build_display_id(d, idx, serial_counts),
        })
    return out


def test_display_id_unique_serial():
    """One device with a non-empty serial uses the serial as display_id."""
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "MYDW-AV1234"},
    ])
    assert devs[0]["display_id"] == "MYDW-AV1234"


def test_display_id_duplicate_serial_disambiguated_by_index():
    """Two DSP-408s sharing a serial get a '#<index>' suffix."""
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "DUP"},
        {"path": b"/dev/hidraw2", "serial_number": "DUP"},
    ])
    ids = [d["display_id"] for d in devs]
    assert ids == ["DUP#0", "DUP#1"]
    assert len(set(ids)) == 2


def test_display_id_empty_serial_falls_back_to_path_hash():
    """Devices without a serial get a stable hash-based id."""
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": ""},
        {"path": b"/dev/hidraw1", "serial_number": ""},
    ])
    assert devs[0]["display_id"].startswith("dsp408-")
    assert devs[1]["display_id"].startswith("dsp408-")
    assert devs[0]["display_id"] != devs[1]["display_id"]


def test_resolve_selector_by_index():
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "A"},
        {"path": b"/dev/hidraw1", "serial_number": "B"},
    ])
    assert _resolve_selector(1, devs)["serial_number"] == "B"
    assert _resolve_selector("0", devs)["serial_number"] == "A"


def test_resolve_selector_by_serial():
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "A"},
        {"path": b"/dev/hidraw1", "serial_number": "B"},
    ])
    assert _resolve_selector("B", devs)["path"] == b"/dev/hidraw1"


def test_resolve_selector_by_display_id():
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "DUP"},
        {"path": b"/dev/hidraw1", "serial_number": "DUP"},
    ])
    assert _resolve_selector("DUP#1", devs)["path"] == b"/dev/hidraw1"


def test_resolve_selector_by_path_string():
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": ""},
    ])
    assert _resolve_selector("/dev/hidraw0", devs) is devs[0]


def test_resolve_selector_default_is_first():
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "A"},
        {"path": b"/dev/hidraw1", "serial_number": "B"},
    ])
    assert _resolve_selector(None, devs) is devs[0]


def test_resolve_selector_empty_bus_raises():
    with pytest.raises(DeviceNotFound):
        _resolve_selector(None, [])


def test_resolve_selector_unknown_raises_with_hint():
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "A"},
    ])
    with pytest.raises(DeviceNotFound) as excinfo:
        _resolve_selector("does-not-exist", devs)
    assert "A" in str(excinfo.value)  # hint lists available ids


def test_resolve_selector_out_of_range():
    devs = _enum_from_raw([
        {"path": b"/dev/hidraw0", "serial_number": "A"},
    ])
    with pytest.raises(DeviceNotFound):
        _resolve_selector(5, devs)
