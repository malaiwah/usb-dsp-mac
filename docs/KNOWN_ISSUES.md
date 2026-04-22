# Known firmware / driver quirks

Empirical findings from live-hardware regression testing on
`MYDW-AV1.06` (v1.06), 2026-04-22.  All of these are firmware-level
quirks; the library's job is to either paper over them transparently
or document them so callers can.

See `tests/live/` for the live-hardware regression suite that locks in
each of these characterisations.

## 1. Read-divergence on early-session reads

**Where:** `read_channel_state(ch)` — the 296-byte multi-frame read at
`cmd=0x77NN`.

**Symptom:** In the first few reads of each channel in a fresh
session, the firmware occasionally returns a blob whose upper bytes
(offsets ≈48..245, the EQ band region) are 2 bytes left-shifted from
the device's actual stored state.  Measured rate on one device:
**6 out of the first 100 reads of ch3, all within the first 6
reads**; 0/94 thereafter.  The semantic per-channel record (mute /
gain / delay / crossover / routing / compressor / name at offsets
244..293) was **not** affected in any observed trial.

**Consequence:** byte-exact diffs of read blobs across a write
boundary can falsely flag "cross-channel mutation" when all that
happened was the baseline snapshot caught a shifted variant.  This
was the root cause of most of the originally-reported "bugs" (cross-
channel wipe from `set_routing`, silent no-op of `set_eq_band` for
`ch>0 band=0`, etc.) — none of those were real.

**Library fix** (`dsp408/device.py::read_channel_state`):
- `Device.connect()` does a warmup pass (2 rounds × 8 channels) to
  drain the cold-read window.
- `read_channel_state()` defaults to an **adaptive** read-until-
  stable: it re-reads up to 6 times, returning the first blob that
  matches the previous blob (ignoring the per-read counter at byte
  294).  In practice converges in 2–3 attempts on warm channels.
- Pass `double_read=False` to skip the adaptive retry (MQTT live-
  status path uses this for lower latency; the divergence affects
  the EQ region which MQTT doesn't poll).

**Wireshark visible:** A dissected live-test capture shows this as
`HPF=256Hz` on channels 0..7 during the connect warmup — pure shift
artifact, not real state.

## 2. Multi-frame WRITE 2-byte payload drop

**Where:** `set_full_channel_state(ch, blob)` — the 296-byte multi-
frame write at `cmd=0x10000..0x10003` (ch 0..3) / `cmd=0x04..0x07`
(ch 4..7).

**Symptom:** The firmware appears to consume only 48 bytes of payload
from the first HID frame even though the capture shows 50, then
starts continuation frames at logical-payload offset 48.  Net effect:
bytes 48..49 of the input blob are **silently dropped** and
everything after gets left-shifted by 2 bytes in internal storage.

**Windows GUI exhibits the same behavior** — replaying the captured
GUI bytes verbatim produces the same readback, so this is a firmware
bug, not ours.

**Workaround:** pad `blob[48..49]` to match `blob[50..51]` before
writing, so the 2-byte drop becomes invisible.  Or don't use
`set_full_channel_state` for partial updates — use the surgical
setters (`set_channel`, `set_routing`, `set_crossover`,
`set_eq_band`, `set_compressor`, `set_channel_name`) instead.

## 3. EQ bands 6..9 storage layout is not `band * 8`

**Where:** `set_eq_band(ch, band=6..9, ...)` writes.

**Symptom:** Writes to bands 6..9 are accepted by the firmware and
round-trip via the library, but the values don't appear at the
expected offsets `blob[band * 8 .. band * 8 + 4]`.  Bands 0..5 do
use the `band * 8` stride and are verified surgical.

**Why:** The Windows GUI only exposes 6 PEQ bands per channel, and
the firmware's internal storage for the remaining "leon-style" 4
bands (if they exist at all) is not at simple 8-byte strides.
Decoding left for a future reverse-engineering session.  See
`notes/blob-layout-verification.md` on the `reverse-engineering`
branch.

**Consequence for users:** use bands 0..5.  Writes to 6..9 aren't
forbidden, but readback won't line up with your intent.

## 4. Byte 294 is a per-read counter

**Where:** Every 296-byte channel-state blob.

**Symptom:** Byte 294 increments with every read even when the
channel state hasn't changed.  It's not a state field — just a
liveness counter.

**Consequence:** Tests that diff blobs across reads must mask byte
294, or they'll see phantom changes on every read.  The library's
internal adaptive-read logic already ignores byte 294 when checking
read convergence.

## 5. Compressor block is inert in firmware v1.06

**Where:** `set_compressor(ch, attack, release, threshold, ...)` —
the write at `cmd=0x2300+ch` targeting blob[278..285].

**Symptom:** Writes land byte-exactly in the blob, but audio
compression is not applied at any parameter combination.
Four-way confirmation (live audio rig, firmware disasm, leon source,
Windows UI) shows the block is inert.

**Consequence:** `set_compressor` round-trips correctly for state-
storage purposes but doesn't affect audio.  MQTT / UI can still
expose these controls if you want to preserve user-set values
across sessions, but don't advertise them as "compression works".

## 6. Compressor shadow at offsets 270..277

**Where:** 296-byte channel-state blob, offsets 270..277.

**Symptom:** Reads from this region return identical default values
to the active compressor record (offsets 278..285) but writes
targeting 270..277 are silently ignored — it's a read-only firmware
"factory defaults" shadow.

**Consequence:** Always write compressor params to 278..285 via
`set_compressor`.  Reading 270..277 is useful for
"what-were-the-defaults" diagnostics but nothing else.

## 7. Test-session-order quirk: `set_eq_band(ch∈{5,6,7}, band=0)` drops late in a rapid chain

**Where:** `set_eq_band()` writes with `channel ∈ {5, 6, 7}` and
`band = 0` specifically, ONLY when they run as part of a long rapid
sequence of other `set_eq_band` writes.

**Symptom:** The write is accepted (WR_ACK received) but the value
doesn't appear in the blob on the next read.

**Reproducibility:**
- **Direct isolated probe** (fresh session, reset defaults, write,
  read) → ALL channels × band 0 round-trip correctly.
- **Batch test** (`test_all_eq_verified_positions_round_trip`,
  writes all 48 positions sequentially then reads) → all 48 land.
- **Interleaved test chain** (`test_set_eq_band_lands_correctly`
  parametrised [band, ch]) → consistently fails for band=0,
  ch∈{5,6,7}, passes for all other combinations.

**Status:** Not a library bug per the first two reproduction paths.
Possibly a firmware cmd-dispatch history effect (cmd=0x10005..0x10007
share low 16 bits with full-channel-state cmd=0x04..0x07 for
ch4..ch7).  The 3 affected test cases are marked `xfail` in
`tests/live/test_surgical_writes.py::test_set_eq_band_lands_correctly`
with a pointer to this note.  Revisit if/when the Wireshark dissector
+ byte-level replay against Windows captures (planned follow-up)
reveals what the GUI does differently.

## 8. Cross-session persistence requires `save_preset()` for flash storage

**Where:** Every mutating public API (`set_channel`, `set_routing`,
`set_crossover`, `set_eq_band`, `set_channel_name`, etc.).

**Symptom:** Writes land in RAM and persist across a
`Device.close() + Device.open()` within the same USB power cycle
(verified by `tests/live/test_persistence_reopen.py`).  But they do
NOT necessarily survive a USB power cycle (yank + replug).  Cross-
power-cycle persistence is via the firmware's preset subsystem —
calling `save_preset(name)` commits the current state to internal
flash.

**Consequence:** If your application needs tuning to survive reboots
of the DSP, call `save_preset()` after whatever change you made.
The library defaults to "write to RAM only" because that's what the
Windows GUI does for live tweaks, and preset-slot writes are a
user-intent action.

## Wireshark visibility

Every one of these quirks is visible in Wireshark captures via the
provided dissector (`tools/wireshark/dsp408.lua`).  A live-test run
(`tests/live/` against the device on `raslabel`) produces a 12,000+
frame capture that dissects cleanly end-to-end, including multi-
frame reassembly and semantic decoding of every `set_*` write's
payload.  If a new quirk is reported, the first diagnostic step
should be:

1. USBPcap (Windows) or `usbmon` (Linux) the traffic while
   reproducing.
2. Open in Wireshark with the dissector loaded.
3. Compare command sequence to a known-good capture on the
   `reverse-engineering` branch (`captures/full-sequence.pcapng` is
   the gold standard).
