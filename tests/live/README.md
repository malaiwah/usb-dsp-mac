# Live-hardware regression tests

These pytest files only pass if a real Dayton Audio DSP-408 is attached
via USB.  They are **default-skipped** on any machine that doesn't set
the live gate, so CI (which has no hardware) stays green.

They are the empirical-truth anchor for the library: every public write
API has one test that writes a distinctive value, reads the 296-byte
channel-state blob, and asserts that **only** the bytes that are
supposed to change actually changed — no cross-channel side effects, no
spurious mutations.  This caught every one of the five "bugs" that
originally looked like real cross-channel corruption (see
``docs/KNOWN_ISSUES.md``): all but one turned out to be read-divergence
artifacts, and the one remaining real quirk
(``read_channel_state`` returning a shifted blob on early-session
reads) is locked in by `test_read_stability.py` and transparently
fixed by the library's default double-read.

## Running

```bash
# Minimum — non-destructive tests, uses first device found
DSP408_LIVE=1 pytest tests/live/

# Or target a specific device by display_id / serial / friendly alias
DSP408_LIVE=1 DSP408_DEVICE="Living Room Subs" pytest tests/live/

# Include the POTENTIALLY DESTRUCTIVE preset-save/load/factory-reset
# tests (second env-var gate required — these overwrite your saved
# preset slot and leave the device in Custom after factory-reset)
DSP408_LIVE=1 DSP408_ALLOW_PRESET_WRITE=1 pytest tests/live/
```

The tests carefully capture and restore per-channel state, so running
them on a device with a live preset is safe — each test snapshots
baseline state, perturbs, then restores before exiting.  If the Python
process is killed mid-test the device may be left with a perturbed
parameter; easiest recovery is to power-cycle the DSP or load a saved
preset.

**Do not run concurrently with the MQTT bridge**
(``systemctl stop dsp408-mqtt`` on the Pi before running, then
``systemctl start`` after).  Both processes try to open the same HID
device exclusively.

## What each file covers

- ``test_read_stability.py`` — characterises the firmware's
  read-divergence quirk and verifies the library's double-read fix.
- ``test_surgical_writes.py`` — every high-level write API touches
  only its target bytes, no cross-channel damage.
- ``test_sequential_writes.py`` — N sequential writes to the same
  channel all land (regression for the bogus "writes stop landing
  after ~5" report).
- ``test_persistence_reopen.py`` — writes survive a
  ``Device.close()`` + reopen within the same power cycle.
- ``test_eq_band_all_positions.py`` — every ``(channel, band)``
  combination is writable (regression for the bogus
  ``set_eq_band(ch>0, band=0)`` report).
- ``test_full_channel_state_shift.py`` — documents the real
  ``set_full_channel_state`` quirk: firmware drops 2 bytes of
  multi-frame WRITE payload, causing a left-shift of the tail.
  Pad blob[48..49] to match [50..51] to make the loss invisible.
