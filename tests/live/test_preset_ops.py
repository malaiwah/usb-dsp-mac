"""POTENTIALLY DESTRUCTIVE: preset save/load live tests.

These tests overwrite the device's internal-flash preset slot.  That
is what presets are for — but if you rely on a carefully-tuned saved
preset on this device, back it up first (USB capture or ``.jssh``
export via the Windows GUI).

Gated by a second env var in addition to ``DSP408_LIVE=1``:

    DSP408_LIVE=1 DSP408_ALLOW_PRESET_WRITE=1 pytest tests/live/test_preset_ops.py

Without ``DSP408_ALLOW_PRESET_WRITE=1`` every test in this file skips.

## What's verified

- ``save_preset(name)`` commits the current in-RAM state to flash
  under ``name`` and makes that name appear in ``read_preset_name()``
  after the operation completes.
- ``load_preset_by_name(name)`` is accepted by the device without
  error (wire-level contract).  Full behavioural testing of "load
  restores exact prior state" is harder because the firmware's
  preset-load path is only partly decoded (see the
  ``load_preset_by_name`` docstring in ``dsp408.device``).
- Saving a preset under a KNOWN name and then reading a perturbed
  parameter still reflects the perturbation (i.e. saving doesn't
  wipe the current in-RAM state).
- Factory reset via ``factory_reset()`` is accepted by the device
  (wire-level); see notes for the caveats about what it actually
  resets.

## What's NOT verified here (manual, out-of-band)

- Cross-power-cycle persistence of saved presets.  Pull USB, plug
  back in, verify preset name + any perturbed parameter survived.
  This is a human-in-the-loop verification — ``tests/live/README.md``
  documents the procedure.
"""
from __future__ import annotations

import os
import struct
import time

import pytest

# Second-tier gate: skip unless user explicitly opted into preset writes
_DESTRUCTIVE = os.environ.get("DSP408_ALLOW_PRESET_WRITE") == "1"
pytestmark = pytest.mark.skipif(
    not _DESTRUCTIVE,
    reason="set DSP408_ALLOW_PRESET_WRITE=1 to run potentially "
           "destructive preset save/load tests",
)

# Distinctive preset name we use for the tests — chosen to be
# recognisable and unlikely to collide with a user-created name.
TEST_PRESET_NAME = "dsp408-py-test"


def test_save_preset_updates_readable_name(dsp):
    """After save_preset('xxx'), read_preset_name() returns 'xxx'."""
    try:
        dsp.save_preset(TEST_PRESET_NAME)
        # The save is a multi-step transaction (trigger, set name,
        # bulk-dump all 8 channels).  Give the firmware enough time
        # to settle before reading.
        time.sleep(1.0)
        got = dsp.read_preset_name()
        assert got == TEST_PRESET_NAME, (
            f"after save_preset({TEST_PRESET_NAME!r}), read_preset_name() "
            f"returned {got!r} instead"
        )
    finally:
        pass  # no restore — the device's preset name is now our test name


def test_save_preset_does_not_wipe_ram_state(dsp):
    """Saving a preset commits state to flash but must not wipe the
    current in-RAM state.  We set a distinctive gain on ch2, save the
    preset, and check the gain is still there after save."""
    want_db = -4.5
    try:
        dsp.set_channel(2, db=want_db, muted=False, delay_samples=0)
        time.sleep(0.3)
        dsp.save_preset(TEST_PRESET_NAME)
        time.sleep(1.0)
        blob = dsp.read_channel_state(2)
        raw = struct.unpack("<H", blob[248:250])[0]
        after_db = (raw - 600) / 10.0
        assert abs(after_db - want_db) < 0.05, (
            f"save_preset wiped ch2 gain from {want_db} to {after_db}"
        )
    finally:
        dsp.set_channel(2, db=0.0, muted=False)


def test_load_preset_by_name_is_accepted(dsp):
    """load_preset_by_name() must not raise and must return without
    leaving the device in an unusable state (verified by doing a
    status read after)."""
    try:
        dsp.load_preset_by_name(TEST_PRESET_NAME)
        time.sleep(0.5)
        # After load, the device should still answer basic queries
        info = dsp.get_info()
        assert "MYDW" in info or info, (
            f"device failed to answer get_info after load_preset_by_name: "
            f"got {info!r}"
        )
    finally:
        pass


def test_factory_reset_accepted_and_device_responsive(dsp):
    """factory_reset() is accepted without error and the device
    remains responsive afterwards.  Does NOT assert "everything is
    back to defaults" because the firmware's reset semantics are
    only partially verified (see the factory_reset docstring)."""
    try:
        dsp.factory_reset()
        # ~500 ms ack time on the magic frame; extra headroom
        time.sleep(1.5)
        # Basic health check: device answers identity string
        info = dsp.get_info()
        assert info, "device unresponsive after factory_reset"
        # Preset name should be "Custom" per the reset sequence
        name = dsp.read_preset_name()
        assert "Custom" in name or name == "Custom", (
            f"post-reset preset name: {name!r} (expected 'Custom')"
        )
    finally:
        # Leave the device in "Custom" reset state — user can
        # re-load a preset or re-save from the Windows GUI
        pass
