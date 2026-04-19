# 296-byte channel-state blob — full field layout decoded

Source: leon v1.23 `DataOptUtil.java:1351–1465` (receive-side parser) and `:1684–1798`
(send-side serializer) — both fill/read the same 296-byte buffer the firmware ships
in response to `cmd=0x77NN`.

Cross-verified with our existing parser at offset 246 (mute), 248–249 (vol), 250–251
(delay), 253 (subidx). The fields **align perfectly with a 2-byte-earlier shift** vs
leon's layout, which means **our firmware uses 30 PEQ bands × 8 bytes + 6 bytes header
= 246 bytes** before the basic record, while leon's app expects 31 PEQ bands + no header.

Leon's `subidx` field is named `spk_type` — **speaker-role identifier**. The values
`(0x01, 0x02, 0x03, 0x07, 0x08, 0x09, 0x0F, 0x12)` in our `CHANNEL_SUBIDX` table are
indices into a 25-entry speaker-role table (`null=0, fl_high=1, fl_mid=2, fl_low=3,
fl=4, fr_high=5, fr_mid=6, fr_low=7, fr=8, ...`). Our factory channel mapping is:

| Channel | spk_type | leon role name |
|--------:|--------:|----------------|
| 0 | 0x01 | fl_high (front-left high) |
| 1 | 0x02 | fl_mid (front-left mid) |
| 2 | 0x03 | fl_low (front-left low) |
| 3 | 0x07 | fr_low (front-right low) — note: skips fl=4, fr_high=5, fr_mid=6 |
| 4 | 0x08 | fr (front-right full?) |
| 5 | 0x09 | (rear-left high?) |
| 6 | 0x0F | (rear-right something?) |
| 7 | 0x12 | (sub or aux?) |

Device 1's ch1 reading `spk_type=0x12` (instead of factory 0x02) means it was
deliberately reconfigured. Selecting a different speaker role from the app's
sound-template dropdown writes this byte AND cascades crossover/EQ defaults.

## Provisional 296-byte layout (our firmware variant)

| Offset | Size | Field | Notes |
|-------:|----:|-------|-------|
| 0 | 240 | EQ bands | 30 bands × 8 bytes? Needs verification — leon has 31 bands here. **TBD: confirm band count by reading PEQ-tweak captures.** |
| 240 | 6 | reserved/header | Possibly per-channel preamble (channel-id, version, flags). **TBD.** |
| **246** | **1** | **mute** (`en_bit`) | **Verified.** 0=muted, 1=audible. Note: INVERTED relative to leon's `mute` field semantics (leon: 1=mute, 0=audible). |
| 247 | 1 | polar | **NEW.** 0/1 phase invert. Not currently parsed. |
| **248–249** | **2** | **gain_le16** | **Verified.** Raw `(dB×10)+600`, range 0..600 = -60..0 dB. |
| **250–251** | **2** | **delay_le16** | **Verified.** Samples (or cm-step index). |
| 252 | 1 | eq_mode | **NEW.** EQ enable/bypass flag, possibly. |
| **253** | **1** | **spk_type** (was "subidx") | **Verified, renamed.** Speaker-role identifier 1..25. |
| 254–255 | 2 | h_freq_le16 | **NEW.** HPF cutoff Hz (or table index). |
| 256 | 1 | h_filter | **NEW.** HPF type: 0=Butterworth, 1=Bessel, 2=Linkwitz-Riley. |
| 257 | 1 | h_level | **NEW.** HPF slope: 0..7 = 6/12/18/24/30/36/42/48 dB/oct, 8=Off. |
| 258–259 | 2 | l_freq_le16 | **NEW.** LPF cutoff. |
| 260 | 1 | l_filter | **NEW.** LPF type. |
| 261 | 1 | l_level | **NEW.** LPF slope. |
| 262–277 | 16 | mixer IN1..IN16 cells | **NEW.** One u8 per input source = level percent (0..100). 16-input firmware capacity exposes IN9..IN16 as zero on our 4-input device. |
| 278–279 | 2 | allPassQ_le16 | **NEW.** Allpass filter Q. |
| 280–281 | 2 | attackTime_le16 | **NEW.** Compressor attack ms. |
| 282–283 | 2 | releaseTime_le16 | **NEW.** Compressor release ms. |
| 284 | 1 | threshold | **NEW.** Compressor threshold (dB encoding TBD). |
| 285 | 1 | linkgroup_num | **NEW.** Channel link/group index for ganged control. |
| 286–293 | 8 | name | **NEW.** ASCII per-channel label (e.g. "TWEETER"). |
| 294–295 | 2 | encryption flag / reserved | leon overwrites byte 288 with `Define.EncryptionFlag` when encrypted preset mode is on. We can ignore. |

## Leon's authoritative layout (for comparison / 31-band firmware variant)

```
0..247   31 EQ bands × 8 bytes
248      mute      (u8, 1=mute, 0=audible — opposite polarity from ours!)
249      polar     (u8)
250–251  gain_le16
252–253  delay_le16
254      eq_mode
255      spk_type
256–257  h_freq_le16
258      h_filter
259      h_level
260–261  l_freq_le16
262      l_filter
263      l_level
264–279  IN1..IN16 mixer cells (16 × u8)
280–281  allPassQ_le16
282–283  attackTime_le16
284–285  releaseTime_le16
286      threshold (u8)
287      linkgroup_num (u8)
288–295  name[8]
```

## EQ band format (8 bytes per band, regardless of count)

```
[0..1]  freq_le16    (Hz, raw 16-bit; firmware uses 332-entry table 20Hz..20kHz)
[2..3]  level_le16   (signed dB×10; bit 15 = sign bit)
[4..5]  bw_le16      (Q index into 100-entry table 0.4..128.0)
[6]     shf_db_u8    (extra dB for shelf mode)
[7]     type_u8      (0=peak, 1=lowshelf, 2=highshelf, …)
```

Our firmware likely supports **10 active bands** (matches manual) but allocates space for
all 30+ — the unused band slots will be zeros. **Verify by reading a fresh device blob
and checking how many bands have non-zero `freq` fields.**

## Frame format — `cmd_le32` decomposition

The 4-byte `cmd_le32` field at offsets 8–11 of our HID frame is actually:

```
byte 8  = CID     (channel index, 0..7 for OUTPUT cmds)
byte 9  = DataID  (parameter group / operation: 0x77 read, 0x1F write basic, ...)
byte 10 = BTD     (always 0 in our captures — purpose unknown, maybe band index)
byte 11 = PCC     (always 0 in our captures — possibly preset/checksum)
```

Combined with the existing decode of bytes 4 (FT/direction) and 7 (DT/category), every
field of the frame now has a known structural meaning.

| Our `cmd` value | DT (cat) | CID | DataID | What it does |
|---:|---:|---:|---:|---|
| `0x7700..0x7707` | 0x04 | 0..7 | 0x77 | Read channel state (296 bytes) |
| `0x1F00..0x1F07` | 0x04 | 0..7 | 0x1F | Write basic record (8-byte payload) |
| `0x2100..0x2107` | 0x04 | 0..7 | 0x21 | Write routing row (4 input levels) |
| `0x05` | 0x09 | 5 | 0x00 | Master volume + mute |

**Hypotheses to test for new commands:**

| Proposed cmd | DT | CID | DataID | What it should do (per leon) |
|---:|---:|---:|---:|---|
| `0x?020 + ch` | 0x04 | 0..7 | 0x20 | Write per-channel crossover (8-byte payload from layout above) |
| `0x?000..0x?00C + ch` | 0x04 | 0..7 | 0x00..0x1E | Write single PEQ band N (8-byte payload) |
| `0x2300..0x2307` | 0x04 | 0..7 | 0x23 | Write 16-input mixer row (16-byte payload) — extends our 0x21 |
| `0x2400..0x2407` | 0x04 | 0..7 | 0x24 | Write compressor params (8-byte payload) |
| `0x2400 + ch` etc. | various | | | Write channel name (8 bytes ASCII) |

The exact opcodes MUST be verified by sniffing Windows-USB captures while the user
clicks PEQ/crossover/compressor in the official app. **We have one capture file:
`captures/full-sequence.pcapng`** — re-grep it for `0x04` category writes that aren't
`0x1F`/`0x21` and look at the payload structure.

## Implementation roadmap

### Phase 1 — read-side decode (pure additive, low risk)

Files to touch in `dsp408/`:
- `device.py` — extend `parse_channel_state_blob()` to return all the new fields.
  Output dict gains: `polar`, `eq_mode`, `h_freq`, `h_filter`, `h_level`, `l_freq`,
  `l_filter`, `l_level`, `mixer` (list of 16), `attack_ms`, `release_ms`, `threshold`,
  `link_group`, `name`, `eq_bands` (list of 10–31 dicts).
- `protocol.py` — add HPF/LPF type enums, slope enum, EQ band enum.
- Tests in `tests/test_controls.py` — synthetic blob fixtures for each new field.

Then extend `mqtt.py:DeviceWorker`:
- `polar` → switch entity per channel
- `h_freq`/`h_filter`/`h_level` → number/select/select per channel (HPF group)
- `l_freq`/`l_filter`/`l_level` → number/select/select per channel (LPF group)
- `mixer[0..3]` levels → number entity per cell (replaces our boolean switches)
- `name` → text entity per channel
- `attack_ms`/`release_ms`/`threshold` → number entities per channel (compressor)
- 10 PEQ bands × `freq`/`level`/`bw` per channel → number entities (could be a lot of HA noise; consider aggregating into one JSON sensor instead)

**Test plan**: with service stopped on Pi, dump 8 channel blobs, parse with new code,
print human-readable. Verify all fields look sensible (HPF freq ≈ 20 Hz on full-range
channels, etc.). Then start service and confirm HA shows the new entities.

### Phase 2 — write paths for the most-wanted fields (still low risk)

In priority order:
1. **Phase invert** — `set_channel_polar(ch, invert: bool)`. Single byte at offset 247
   of basic-record-extended write. May reuse the existing `0x1F` opcode with longer
   payload, OR a new opcode like `0x22` — TBD by sniffing.
2. **Crossover** — `set_channel_crossover(ch, hpf=(type, freq, slope), lpf=...)`.
3. **Streaming on/off** — `set_streaming(on: bool)`. From leon: register 1555.
4. **Factory reset** — `factory_reset()` writes magic `0xA5A6` to register 1567.
5. **Compressor** — `set_channel_compressor(ch, attack_ms, release_ms, threshold_db)`.
6. **PEQ bands** — `set_channel_eq_band(ch, band_idx, freq_hz, gain_db, q, type)`.

Each one follows the same pattern: one `write_raw()` call with the right cmd + payload.

### Phase 3 — MQTT exposure for write paths

Wire each new `set_*` method into a corresponding HA discovery component + MQTT
command-topic handler in `DeviceWorker.handle_command()`.

### Phase 4 — verification harness

A scripted "round-trip every field" test: write a known value, read back, assert match.
Run it on the Pi with both devices to catch firmware-variant differences.

## Sniffing plan to pin down the unknown opcodes

We need fresh USB captures of the Windows app doing:
1. Toggle phase invert on a channel
2. Set HPF type/freq/slope on a channel
3. Set LPF type/freq/slope on a channel
4. Adjust a mixer cell from 0% to 50% (verify it's u8 percentage, not boolean)
5. Set compressor attack / release / threshold
6. Edit one PEQ band (freq, gain, Q)
7. Save preset to slot 2
8. Recall preset from slot 2

Then in `captures/`, decode the writes that fall outside our known cmds. Pair each
captured cmd with the UI action that triggered it. That pins down the remaining
opcodes without guessing.

## ESP32 stretch — replacing the Pi with a USB-host MCU

The DSP-408 is **USB-device** (it's the "B" port). To control it without a Pi we need
a USB-**host** MCU. Options:

1. **ESP32-S3** — has native USB OTG (host capable). Run TinyUSB or Espressif's USB
   Host Library, implement the HID class. ESPHome has a `usb` component but it's still
   experimental. ~$10 board, plus a USB-A-to-USB-B cable.
2. **ESP32-P4** (newer) — better USB host support, but harder to source.
3. **ESP32-S2** — has USB OTG too but only one core; less headroom.

A thin C++ ESPHome external_components package would mirror our `dsp408/protocol.py`
+ `device.py` + `mqtt.py`:
- TinyUSB HID host attaches to VID=0x0483 PID=0x5750
- Send/receive 64-byte HID reports with our magic-`80 80 80 EE` framing
- Each control surface becomes an ESPHome `number`/`switch`/`select` entity, which
  ESPHome auto-publishes to HA via the native API or MQTT
- Wi-Fi/Ethernet/BLE all work out of the box on ESP32

This effectively turns one ESP32 into a "Dayton DSP-408 → Wi-Fi bridge". Plug it into
the DSP's USB port, plug in 5V, and HA sees the device.

**Risk areas for the port:**
- The DSP enumerates as HID *and* a vendor interface — TinyUSB host needs a custom
  driver hook that bypasses the report descriptor and just shoves 64-byte reports
  through interrupt OUT. Doable but not boilerplate.
- Multi-frame reads (296-byte blob across 5 HID reports) need timing care.
- 8-channel firmware update (`0x36/0x37/0x38/0x39` flash sequence) requires ~70 KB of
  flash reads from the SD card or HTTP — feasible on ESP32-S3.

A sensible MVP: port `protocol.py` + `device.py` to C++, expose only master volume +
8× channel volume + mute as a proof of concept, then add the rest. Skip the firmware-
update path entirely (let the Pi/Mac do that out-of-band).

## Files to keep open while implementing

In leon decompile (`/tmp/dsp408-apk/jadx-leon/sources/leon/android/chs_ydw_dcs480_dsp_408/`):
- `operation/DataOptUtil.java:1340–1798` — the canonical receive + send serialization
  for the 296-byte channel buffer. Lines 1351–1465 = parser, 1684–1798 = serializer.
- `datastruct/DataStruct_Output.java` — list of all per-output fields
- `datastruct/DataStruct_System.java` — system-wide fields (master vol, sub vol, etc.)
- `datastruct/DataStruct_Input.java` — per-INPUT processing (yes, inputs have full DSP too!)
- `datastruct/DataStruct_EQ.java` — per-band fields
- `datastruct/Define.java` — opcode + flag constants

In tigerapp decompile (`/tmp/dsp408-apk/jadx-real/sources/com/tigerapp/rkeqchart_application_1/`):
- `f/a.java` — register address tables (different opcode space, but useful sanity check)
- `g/a.java` — value encodings (dB ↔ raw, Hz ↔ table index, Q ↔ table index)

In our codebase (`/Users/mbelleau/Code/usb_dsp_mac/dsp408/`):
- `protocol.py` — frame format + opcode constants
- `device.py:parse_channel_state_blob()` — currently extracts 4 fields, target ~25
- `mqtt.py:DeviceWorker.build_discovery_payload()` — one new entity per new field

---

## ESP32 stretch — correction (added after dual-USB confirmation)

**Earlier scoping was wrong.** The DSP-408 has TWO USB ports, not one:

- **USB-B (back)** — the HID device port we use today. Our driver is the host.
- **USB-A (front)** — a USB **host** port for the DSP-BT4.0 dongle.
  Confirmed by firmware analysis: `STM32 USB_OTG_FS` peripheral at `0x50000000` is
  referenced 3 times in `downloads/DSP-408-Firmware-V6.21.bin`, separate from the
  USB-device peripheral at `0x40005C00` that drives the back port.

That changes the ESP32 path entirely. The ESP32 just needs to be a USB **device**
pretending to be a DSP-BT4.0 dongle on the front port. Every ESP32-S2/S3/C3 (and
indeed ESP32-C6, RP2040, etc.) does USB-device mode out of the box — no TinyUSB host,
no exotic chip selection. **An ESP32-S2 at ~$3 is enough.**

What we still need to learn:

1. **What USB class does the DSP-BT4.0 dongle present?** Most likely options:
   - CDC-ACM (USB virtual serial port — typical of cheap BLE dongles built on CC2540 + CH340)
   - Vendor-specific bulk endpoints (raw byte stream)
   - Less likely: HID
2. **What VID/PID does the firmware look for?** The host enumeration code in firmware
   (search for `USB_OTG_FS_HOST_BASE` reads + USB descriptor parsing routines) will
   tell us if it accepts any CDC device or only a specific Dayton VID.
3. **What's the wire framing on top of that USB stream?** Almost certainly the same
   `80 80 80 EE | dir | ver | seq | cat | cmd | len | payload | xor | aa` envelope —
   leon v1.23's BLE/SPP code uses exactly this format, and the BT dongle internally
   converts BLE SPP frames to USB. So the front-port byte stream is probably 1:1 with
   our HID frame minus the 1-byte report-ID prefix.

How to confirm without buying a dongle:
- **Firmware analysis**: locate the USB_OTG_FS host driver setup in the firmware,
  identify the class driver it loads (CDC vs vendor) and any VID/PID match logic.
- **Existing captures**: nothing in our `captures/` involves the front port — they're
  all USB-B HID. We'd need a fresh Linux USB capture of the dongle plugged into a PC.

**MVP for the ESP32 sub-project, revised:**
1. Sanity-prove the front-port USB class (above).
2. ESP32-S2 firmware: USB device class matching the dongle, parse `80 80 80 EE` frames.
3. Mirror the Python `device.py` logic: cache state, debounce writes, handle
   master-vol/mute + per-channel vol/mute first.
4. Expose via ESPHome native API or MQTT — every control becomes a number/switch.
5. Wi-Fi configures via WiFiManager captive portal; HA discovery just works.

Roughly **1 weekend of work once the USB class is confirmed**, vs the ~1 week of
TinyUSB-host pain I estimated when I thought we needed to USB-host the DSP-408.

The hardware story becomes: plug ESP32-S2 dev board into the DSP's front USB-A port
(use a USB-A male to USB-A male cable, or solder the ESP directly to a USB-A plug).
Power can come from either the DSP's port (5V is on the front USB) or from a separate
USB power supply. No Pi, no MQTT broker setup unless you already have one — ESPHome's
native API is enough.
