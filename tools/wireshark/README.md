# DSP-408 Wireshark Dissector

A Wireshark Lua dissector for the Dayton Audio DSP-408 (VID=0x0483 PID=0x5750)
USB HID protocol. Decodes captures into human-readable actions in the packet
list and into labeled fields in the packet details pane.

Mirrors the protocol definitions in [`dsp408/protocol.py`](../../dsp408/protocol.py).

## Install

Copy `dsp408.lua` into your Wireshark personal plugins directory:

| OS                 | Path                                                                      |
| ------------------ | ------------------------------------------------------------------------- |
| Linux              | `~/.local/lib/wireshark/plugins/` (Wireshark ≥ 4.0) or `~/.config/wireshark/plugins/` |
| macOS              | `~/.config/wireshark/plugins/` or `~/.local/lib/wireshark/plugins/`       |
| Windows            | `%APPDATA%\Wireshark\plugins\`                                            |

Then either restart Wireshark or use **Tools → Lua → Reload Lua Plugins** /
`Ctrl+Shift+L`.

The exact path Wireshark scans is shown in **Help → About Wireshark → Folders →
Personal Lua Plugins**.

### One-liner (Linux/macOS)

```sh
mkdir -p ~/.config/wireshark/plugins
cp tools/wireshark/dsp408.lua ~/.config/wireshark/plugins/
```

### tshark

Same file works with `tshark`. You can also load it ad-hoc without installing:

```sh
tshark -X lua_script:tools/wireshark/dsp408.lua -r captures/full-sequence.pcapng
```

## Use

Open any DSP-408 USB capture (USBPcap on Windows, `usbmon` on Linux, or
`Wireshark → Capture → USB Bus N` on macOS with the `ChmodBPF` helper). Every
HID report whose first four bytes are `80 80 80 ee` is picked up automatically.

Expected packet-list rendering:

```
WR     seq=0  master                        level=-20 dB
WR     seq=0  routing(ch=0)                 ch=0 IN1=0x64,IN2=0x64,IN3=0x64,IN4=0x64
WR     seq=0  channel(ch=0)                 ch=0 ON vol=-4.4dB delay=0
WR     seq=0  crossover(ch=2)               ch=2 HPF=20Hz/Linkwitz-Riley/12 dB/oct LPF=20000Hz/Butterworth/12 dB/oct
WR     seq=0  eq_band(band=0,ch=2)          band=0 ch=2 f=31Hz gain=+3.8dB Q≈4.92
WR     seq=0  compressor(ch=0)              ch=0 Q=420 attack=56ms release=500ms thresh=0 link=1
WR     seq=0  preset_save_trigger           SAVE preset → flash
WR     seq=0  write_global / factory_reset  FACTORY RESET MAGIC
WR     seq=0  full_channel_state(ch=1)      ch=1 (296-byte blob — see first-frame note) [multi-frame first]
```

After editing `dsp408.lua`, reload via **Tools → Lua → Reload Lua Plugins**.

## Useful display filters

```
dsp408                                  # all DSP-408 frames (including reassembled continuations)
dsp408.direction == 0xa1                # host→device WRITEs only
dsp408.cmd_name contains "routing"      # routing changes
dsp408.cmd_name contains "factory"      # factory-reset magic
dsp408.cmd_name contains "preset_save"  # persist-to-flash triggers
dsp408.checksum_ok == 0                 # broken XOR checksums (red rows with coloring import)
dsp408.payload_len > 48                 # first frames of multi-frame payloads
dsp408.continuation_of                  # multi-frame continuations
dsp408.reassembled_len == 296           # fully-reassembled channel-state blobs
dsp408.eq.gain_db > 0                   # EQ boosts (raw gain_db field)
dsp408.channel.vol_db < -30             # channels attenuated > 30 dB
```

## What's decoded

### Header (every frame)
`magic`, `direction` (WRITE / READ request / READ reply / WRITE ack), `version`,
`seq`, `category` (INPUT / PARAM / STATE), `cmd`, `payload_len`, `checksum`
(validated against XOR of bytes 4..chk-1), `end_marker` (expected `0xaa`).

Bad checksums and missing end markers show as expert-info warnings.

### Commands with human-readable summaries (shown in Info column)

| Cmd range                | Name                  | Summary                                                       |
| ------------------------ | --------------------- | ------------------------------------------------------------- |
| `0x05`                   | `master`              | `level=+N dB [MUTED]`                                         |
| `0x1F00..0x1F07`         | `channel(ch=N)`       | `ch=N ON/MUTE vol=±N.N dB delay=N`                            |
| `0x2100..0x2107`         | `routing(ch=N)`       | `ch=N IN1=0x64,IN2=0x64,...` (IN1..IN8)                       |
| `0x2200..0x2207`         | `routing_hi(ch=N)`    | IN9..IN16 (DSP-408 leaves these zero)                         |
| `0x2300..0x2307`         | `compressor(ch=N)`    | `Q=N attack=Nms release=Nms thresh=N link=N`                  |
| `0x2400..0x2407`         | `channel_name(ch=N)`  | `name="..."` (8-byte ASCII, trimmed)                          |
| `0x10000..0x10FFF`       | `eq_band(band=N,ch=N)` | `f=NHz gain=±N.N dB Q≈N.NN`                                   |
| `0x12000..0x12007`       | `crossover(ch=N)`     | `HPF=NHz/type/slope LPF=NHz/type/slope`                       |
| `0x2000`                 | `factory_reset`       | `FACTORY RESET MAGIC` if payload = `06 1f 00 00 20 4e 00 01`  |
| `0x34` (WRITE)           | `preset_save_trigger` | `SAVE preset → flash` if payload byte = `0x01`                |
| `0x10000..0x10003` (len=296) | `full_channel_state(ch=0..3)` | flagged `[multi-frame first]`                         |
| `0x04..0x07` (WRITE, len=296) | `full_channel_state(ch=4..7)` | flagged `[multi-frame first]`                         |

### State / system commands (named, payload shown as bytes)

`connect` (`0xCC`), `get_info` (`0x04` read), `preset_name` (`0x00`),
`idle_poll` (`0x03`), `status` (`0x34` read), `state_0x13`,
`global_0x02` / `global_0x05` / `global_0x06`,
firmware: `fw_prep` / `fw_meta` / `fw_block` / `fw_apply`.

### Input-side (category 0x03 — MUSIC)

Per-input EQ bands (`input_eq_band(band=N,ch=N)` for band=0..14),
`input_misc`, `input_dataid10`, `input_noisegate`, `read_input_state`.
Command names decoded; payload shown as bytes (semantics only partially
calibrated — see [`dsp408/protocol.py`](../../dsp408/protocol.py) comments).

## Validation

v2 has been run against every `.pcapng` on the `reverse-engineering` branch
(14 captures, including 4 firmware-update runs, USB-enum probes, preset
save/load, hours of interactive edits):

| | Count |
|---|---|
| Total DSP-408 frames decoded | **92,063** |
| Multi-frame firsts detected | **280** |
| Multi-frame groups reassembled | **280** (100%) |
| Bad XOR checksums | 0 |
| Missing end markers | 0 |
| Abandoned multi-frames | 0 |

See the command recipe at the bottom to reproduce.

## Multi-frame reassembly (v2)

As of v2, the dissector reassembles 296-byte payloads (`full_channel_state`
WRITEs on cmd=`0x10000+ch` or cmd=`0x04..0x07`, and `read_channel_state`
READ replies on cmd=`0x7700+ch`) across 5 HID URBs (first + 4 continuations)
and decodes the full channel-state blob: 10 EQ bands, basic record (mute /
polarity / vol / delay / spk_type), crossover (HPF+LPF), mixer, compressor,
and channel name. The last continuation frame shows `→ REASSEMBLED (296
bytes) ch=N …` in the Info column.

### Multi-frame protocol deep dive (verified across 6 captures)

**The only wire-level signal for multi-frame is `payload_len > 48` in the
first frame's header.** No ambient pre- or post-herald exists. All other
invariants are derivable from that one field:

| Invariant | Value |
|---|---|
| First-frame layout | magic(4) + header(10) + 50 payload bytes (no chk/end) |
| Continuation URB count | `ceil((declared_len - 50) / 64)` — 4 for a 296-byte blob |
| Continuations | 64 raw payload bytes each, no framing, no magic, same USB endpoint as the first frame |
| Close bookend | `chk + 0xAA` at HID offset `declared_len - 50 - 64*(N-1)` in the last continuation, followed by zero padding |
| Direction-agnostic | Identical layout for host→dev WRITE and dev→host READ reply |
| Confirmation | WRITE: dev sends `WR_ACK` (with magic) 200-700 ms after last continuation. READ: host moves on. |

### GUI conventions that look like protocol and aren't

The Windows DSP-408.exe app surrounds multi-frame writes with app-level
operations that **are not protocol requirements**. We verified that
different captures use totally different surrounds for the same
multi-frame cmd:

| Operation type (what the user did) | Typical pre-amble | Typical post-amble | Purpose |
|---|---|---|---|
| Interactive single-channel EQ/xover edit | `WR channel(chN) MUTE` | `WR channel(chN) ON` | avoid speaker pops during retune |
| Multi-channel coordinated change | `WR master MUTED` | next channel batched | silence whole output |
| Load preset from disk | `WR preset_name` once | `WR preset_name`/`master` at batch end | bookkeeping |
| **Save preset to slot** | **`WR preset_save_trigger` (cmd=0x34, payload=0x01)** | `WR preset_name` | **persistence** (see below) |

The Python library ([dsp408/](../../dsp408/)) sends multi-frame writes
without any mute/unmute surround and the device acks them identically —
further confirming the surround is cosmetic.

### Persistence: how settings reach flash

The DSP-408 firmware has a **single explicit persist trigger**:

> `cmd=0x34 cat=0x09 dir=0xA1 payload=0x01` — the one-byte `preset_save_trigger`.

Without this trigger immediately before a batch of `full_channel_state`
writes, the blob lands in RAM only and is lost on power cycle. The GUI
emits it exactly once per "Save" button click. Occurrences across captures:

| Capture | Multi-frame writes | `preset_save_trigger` events |
|---|---|---|
| `load_loaddisk_save_preset_bureau` (user saved a preset) | 16 | 1 |
| `windows-04b-volumes-mute-presets` (volume tweaks) | 1 | 0 |
| `full-sequence` (hours of interactive EQ/xover) | 15 | **0** |

**User-visible consequence:** sliders and EQ tweaks in the Windows GUI are
RAM-only until the user clicks Save. Power-cycling the DSP wipes them.
This is firmware behaviour, not a GUI bug.

Spot these in a capture by filtering
`dsp408.cmd_name contains "preset_save_trigger"` — the coloring rules
(see below) paint them red.

## What's NOT decoded

* **The `.jssh` preset file format** — that's a separate decoder (JSON + XOR
  cipher), not USB traffic.
* **288-byte `read_input_state` blobs** (cat=0x03 cmd=0x7700+ch) —
  multi-frame reassembly works but field semantics are only partly
  reverse-engineered. The reassembled bytes are exposed as a
  `dsp408.reassembled` field; see [`dsp408/protocol.py`](../../dsp408/protocol.py)
  CAT_INPUT comments for the known offsets.
* **Bluetooth LE captures** from the Android app — they use the same wire
  format inside GATT writes but a different transport; the dissector would
  need a BT-attribute-PDU child registration.
* **Compressor audio behaviour** — the block's wire format is fully decoded
  (cmd `0x2300+ch`, Q / attack / release / threshold / link), but the
  firmware is inert for this subsystem in v1.06. The decoder shows what was
  written; don't expect to hear it.

## Coloring rules (bad-frame highlighting)

Import `dsp408.colors` via **View → Coloring Rules → Import** to paint:

* Bad magic / bad checksum rows — **red** background
* Abandoned multi-frame first (next magic arrived before last continuation) — **orange**
* `preset_save_trigger` / `factory_reset` — **pink** (persistence actions worth noticing)
* WRITE (host→dev) vs READ (host→dev) vs ACK/reply (dev→host) — gentle tints
* Multi-frame continuations — pale yellow

Expert-info indicators (the colored dot in the Info column) fire independently:
ERROR (red) for bad magic / bad checksum, WARN (yellow) for missing end marker
or abandoned multi-frame, NOTE (cyan) for reassembly notes.

## Registration

The dissector attaches in two ways so you don't have to configure anything:

1. **`usb.product` table** at key `0x04835750` — catches every transfer on a
   matching VID/PID device.
2. **Heuristic on `usb.interrupt` and `usb.bulk`** — catches captures where
   the URB doesn't carry VID/PID metadata (Linux `usbmon`). The heuristic
   accepts a packet only if the first four bytes are the DSP-408 magic.

If you ever see a non-DSP-408 packet being mis-decoded, check the magic —
collisions are effectively impossible given the 4-byte sentinel.

## Known quirks

* **WR_ACK of `full_channel_state` shows as `eq_band(band=0,ch=N)`.** The
  device mirrors cmd `0x10000+ch` in its ack, but with a short payload. Our
  discriminator (`len==296` → full_channel_state) correctly catches the
  WRITE side; the ACK gets the eq_band label. Cosmetic — both refer to the
  same channel.
* **`cmd_0x04` ambiguity.** Reads to `0x04` are `get_info`; writes to `0x04`
  with 296-byte payload are `full_channel_state(ch=4)`. Disambiguated by
  direction + payload length.
* **Category 0x03 field semantics** (input EQ, noisegate) are partly
  reverse-engineered. Command names are stable; field interpretations in
  the tree show raw bytes where semantics are unconfirmed.
* **Multi-frame checksum: display-only on the last continuation.** The XOR
  covers the first-frame header bytes + the full reassembled payload. We
  display the checksum byte from the last continuation but skip strict
  recomputation (would require snapshotting the first-frame header bytes
  through reassembly — v3 if anyone cares). The 0xAA end-marker IS strictly
  validated.
* **Abandoned multi-frame tracking.** If a new magic-bearing frame arrives
  on a conversation before an open multi-frame completes (out-of-order,
  dropped URB, capture started mid-sequence), the first frame is marked
  `ABANDONED` in the Info column and expert-info. In-order, no-drop
  captures — what we see in practice — never trigger this.
* **MAX_CONTINUATION_FRAMES = 12** safety cap in the dissector. Real
  traffic needs 4; the cap is belt-and-braces against a pathological
  first-frame `payload_len` fooling the state machine into consuming
  arbitrary following URBs as continuations.

## Reproducing the validation

```sh
# Sweep every capture on the RE branch, count decode outcomes
for cap in $(git ls-tree -r reverse-engineering --name-only | grep 'captures/.*\.pcapng$'); do
  git show "reverse-engineering:$cap" > /tmp/cap.pcapng
  total=$(tshark -X lua_script:tools/wireshark/dsp408.lua -r /tmp/cap.pcapng -Y 'dsp408' 2>/dev/null | wc -l)
  mfirst=$(tshark -X lua_script:tools/wireshark/dsp408.lua -r /tmp/cap.pcapng -Y 'dsp408.payload_len > 48' 2>/dev/null | wc -l)
  reass=$(tshark -X lua_script:tools/wireshark/dsp408.lua -r /tmp/cap.pcapng -Y 'dsp408.reassembled_len' 2>/dev/null | wc -l)
  badck=$(tshark -X lua_script:tools/wireshark/dsp408.lua -r /tmp/cap.pcapng -Y 'dsp408.checksum_ok == 0' 2>/dev/null | wc -l)
  printf "%-55s total=%s mf=%s reass=%s bad=%s\n" "$(basename $cap)" $total $mfirst $reass $badck
done
```

## Regenerating the screenshot

```sh
tshark -X lua_script:tools/wireshark/dsp408.lua \
  -r captures/full-sequence.pcapng \
  -Y 'dsp408.cmd_name contains "routing" or dsp408.cmd_name contains "crossover" or dsp408.cmd_name contains "eq_band" or dsp408.cmd_name contains "master" or dsp408.cmd_name contains "channel("' \
  | head -40
```

A canned PNG sample lives at [`screenshot.png`](screenshot.png).
