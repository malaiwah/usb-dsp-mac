# Captures still needed from the Windows DSP-408.exe GUI

**Status as of 2026-04-19** (after the EQ-band reverse-engineering pass on the
`loopback-rig` branch wrapped up).

The captures we already have (`captures/windows-04*.pcapng`,
`captures/full-sequence.pcapng`) cover almost every user-facing audio control:
master, per-channel volume / mute / delay / phase, routing matrix, crossover
(HPF + LPF, all 4 filter types × 9 slope values), and 10-band parametric EQ
(freq / gain / Q via the b4 reciprocal byte).  The compressor write cmd
(`0x2300 + ch`, payload `[Q_le16, attack_le16, release_le16, threshold, enable]`)
turned up incidentally inside `windows-04b-volumes-mute-presets.pcapng`.

What's left is roughly: anything the GUI *can* do that the existing captures
never exercised.  Each item below lists what to click and what to look for in
the resulting pcapng so the encoding can be decoded without having to ask
"what should this packet do?" after the fact.

---

## 1. Per-channel name / label  (high value, low effort)

**What we don't know:** the cmd code that writes the 8-byte ASCII channel name
field at `blob[OFF_NAME .. OFF_NAME+8]`.  We've never seen the GUI rename a
channel in any capture.

**Repro steps:**
1.  Connect to the device, let the idle handshake settle (~5 s).
2.  Start capture.
3.  In the GUI, change channel 1's label from its default to a distinctive
    7-bit ASCII string, e.g. `"TWEETER"`.
4.  Pause ~2 s.
5.  Change channel 2's label to a *different* string, e.g. `"WOOFER"`,
    so we can disambiguate channel index encoding.
6.  Pause ~2 s.
7.  Change channel 1 *again* to a third string, e.g. `"MID"`, to
    confirm idempotence + see the byte-for-byte payload twice for the
    same channel.
8.  Stop capture.

**What to look for in the pcapng:**
- A WRITE frame (`dir=a1`, `cat=0x04` likely) whose payload contains the
  literal ASCII bytes `"TWEETER"` (`54 57 45 45 54 45 52`).  Grep with
  `tshark -r ... -T fields -e usbhid.data | grep -i 5457454554` (note: hex
  for "TWEET").
- Cross-reference the cmd code against per-channel writes we already
  know (`0x1F00..0x1F07`, `0x2100..0x2107`, `0x2300..0x2307`).  The
  channel-name cmd is probably `0x22NN` or `0x24NN` — adjacent to the
  others in the per-channel block.

**Validation plan once the encoding is decoded:** loopback rig isn't
needed; we just write the label, read back `blob[OFF_NAME..OFF_NAME+8]`
via `read_channel_state()`, confirm it matches.

---

## 2. Factory reset  (high value, blocked entirely)

**What we don't know:** the wire encoding for the magic-word system register
write that triggers a full factory reset.  The leon Android decompile
(see `notes/android-app-decompile-2026-04-19.md`) shows the Android app
writing magic `0xA5A6` to register address `1567` (`0x061F`), but our
live probes against this encoding all failed (one variant *corrupted ch7
EQ data*, see the cautionary block in `dsp408/device.py::factory_reset`).
**That implementation is the known-broken stub today.**

**Repro steps:**
1.  Connect, let handshake settle.
2.  Make a few visible changes first so we can see "reset took": set
    master to -10 dB, mute ch1, change crossover on ch2 to BW 24 dB/oct
    @ 200 Hz / 8 kHz.  Confirm in the GUI those changed.
3.  Pause ~2 s.
4.  Start capture.
5.  In the GUI, click "Factory Reset" (or whatever it's labelled in
    V1.24 — usually a button in the Tools / System menu).  Confirm the
    confirmation dialog.
6.  Wait ~3 s for the device to settle.
7.  Stop capture.

**What to look for:**
- A unique WRITE frame (`dir=a1`) right around the click — should be a
  one-shot, not a sequence.  Likely cat=`0x09` (state cmd).
- The cmd code may be `0x061F` (the leon register address) or could be a
  totally different cmd code in our HID protocol with the magic value in
  payload.  The Android app talks UART, not USB — the encoding could
  have been remapped.
- Compare against the post-reset 0x77NN reads: every channel should now
  show factory defaults (mute=audible, gain=0 dB, EQ flat, crossover
  20 Hz / 20 kHz BW 12 dB).

**Validation plan once decoded:** rig optional — read all 8 channel
state blobs before/after to confirm reset took.  Audio loopback also
useful as a smoke test (everything should pass through cleanly afterward).

---

## 3. Preset save / load / delete  (high value)

**What we don't know:** how the GUI saves the current state to one of
the 6 named preset slots, loads a preset back, and deletes one.  The
leon decompile mentions a magic constant `0xB500 | preset_id` for
loading factory presets, but we haven't seen this in any USB capture and
the user-preset slots may use different encoding.

**Suspect:** `cmd=0x60 cat=0x09` shows up exactly once in
`windows-04b-volumes-mute-presets.pcapng` at t≈10 s with all-zero
payload, but we can't tell if that's "save preset 0", "preset list
read", or something else from a single sample.

**Repro steps:**
1.  Connect, let handshake settle.
2.  Make a visible state change (e.g. set master to -15 dB, ch1 EQ band 5
    to +6 dB at 1 kHz).
3.  Pause ~2 s.
4.  Start capture.
5.  Click "Save Preset" → name it `"PROBE_A"`, save to slot 1.
6.  Pause ~3 s.
7.  Make a *different* state change (master to -5 dB, ch1 EQ flat).
8.  Pause ~2 s.
9.  Click "Save Preset" again → name `"PROBE_B"`, save to slot 2.
10. Pause ~2 s.
11. Click "Load Preset" → choose slot 1 (`"PROBE_A"`).  GUI state
    should snap back to master=-15, EQ peak.
12. Pause ~3 s.
13. Click "Load Preset" → choose slot 2 (`"PROBE_B"`).
14. Pause ~3 s.
15. Click "Delete Preset" → delete slot 2.
16. Stop capture.

**What to look for:**
- Save: WRITE frame whose payload contains the ASCII preset name
  (`"PROBE_A"` = `50 52 4f 42 45 5f 41`).  This gives the cmd + name
  encoding in one shot.
- Load: WRITE frame with a small payload (probably just the preset
  index byte), followed by the device's spontaneous emit of the new
  state (lots of subsequent reads/state writes from the firmware as it
  reapplies the preset).
- Delete: another WRITE with the preset index, no name payload.

**Validation plan:** rig only needed for end-to-end sanity (apply a
known state, save, change everything, load, confirm restored).  Most
testing can be against blob readbacks.

---

## 4. EQ enable / bypass per channel  (medium value)

**What we don't know:** how the GUI globally bypasses *all* EQ bands on
a channel without zeroing the per-band gains.  We probed `byte[6]` of
the per-channel write payload (cmd=0x1F0X) and confirmed it round-trips
into `blob[OFF_EQ_MODE]=blob[252]` but **does NOT actually disable the
EQ peak** — see `tests/loopback/_probe_eq_mode.py` on the
`loopback-rig` branch.

So the EQ bypass control is somewhere else — possibly a separate cmd
code, or possibly the GUI just zeroes all 10 bands when you click "EQ
off" and re-applies the saved values when you click it back on.  A
capture would tell us which.

**Repro steps:**
1.  Connect, let handshake settle.
2.  On channel 1, set EQ band 5 to a *visible* peak: +12 dB at 1 kHz, Q=5.
    Confirm in the GUI you see the bell shape.
3.  Pause ~2 s.
4.  Start capture.
5.  Click the channel 1 "EQ on/off" toggle to OFF.  The bell shape in
    the GUI should disappear (flatten).
6.  Pause ~3 s.
7.  Click the toggle back to ON.  Bell shape returns.
8.  Pause ~2 s.
9.  Repeat the off/on cycle one more time so we see the encoding twice.
10. Stop capture.

**What to look for:**
- If we see only **one** WRITE frame per toggle, it's a dedicated bypass
  cmd — note the cmd code, payload, and which byte changes between
  on/off.
- If we see **10** WRITE frames per toggle (one per EQ band), the GUI is
  faking it by zeroing/restoring each band's gain.  In that case there
  is no separate bypass control, and our SDK can match the GUI's
  behaviour by caching gains client-side.

**Validation plan:** loopback rig confirms bypass actually flattens the
acoustic response.

---

## 5. Compressor — full parameter sweep  (medium value)

**What we have:** 4 frames in `windows-04b-volumes-mute-presets.pcapng`
that toggled the compressor enable bit on/off for ch6 and ch7, with
fixed values (`Q=420, attack=56 ms, release=500 ms, threshold=0`).  That
nailed the cmd (`0x2300 + ch`) and payload layout, but we don't know:
- What the `Q_le16` field actually does — it might be ratio (1:N), it
  might be the all-pass-Q the leon decompile claims, or it might be
  something else entirely.
- The encoding range of `threshold` (single byte; is it 0..255 mapping
  to 0..-60 dB?  Or signed?  Or dB×10?).
- Whether there's a separate **ratio** field we haven't found.
- What `linkgroup` would do (linking compressors across channels?).

**Repro steps:**
1.  Connect, let handshake settle.
2.  Pick channel 1, enable compressor, leave at default values, observe
    the GUI shows ratio=N:1, threshold=X dB (write down what the GUI
    actually displays so we can map raw bytes ↔ dB / ratio).
3.  Pause ~2 s.
4.  Start capture.
5.  Walk threshold through 4–5 distinct values: e.g. 0, -10, -20, -40,
    -60 dB.  Pause 1 s between each.
6.  Walk ratio through 4–5 distinct values: 1.5:1, 2:1, 4:1, 8:1, ∞:1.
    Pause 1 s between each.
7.  Walk attack through 1, 5, 20, 100, 500 ms.  Pause 1 s between each.
8.  Walk release through 50, 200, 500, 1000, 2000 ms.  Pause 1 s.
9.  If the GUI exposes a "link" or "channel group" control on the
    compressor, walk it through 1, 2, 3 (and then "off"/none).
10. Stop capture.

**What to look for:**
- For each click, exactly one WRITE frame to `cmd=0x230N` cat=0x04.  The
  varying bytes in the payload reveal which field maps to which GUI
  control.
- If ratio shows up in a payload byte that wasn't used in our 4-frame
  sample, that resolves the "is there a ratio field?" question.

**Validation plan:** loopback rig with compressor characterization
script (drive a tone at a known level above/below threshold, measure
output reduction vs time, fit envelope to extract attack/release time
constants).  Same playbook as the EQ Q-sweep we just did.

---

## 6. Speaker-type / channel-role selector  (low value, side-quest)

**What we don't know:** what the speaker-role byte (`OFF_SPK_TYPE` =
`blob[253]`) actually *does* in the firmware.  The leon decompile names
25 roles (`SPK_TYPE_NAMES` in `dsp408/protocol.py`); the factory
defaults set channels 0–7 to a sparse subset (`CHANNEL_SUBIDX = (0x01,
0x02, 0x03, 0x07, 0x08, 0x09, 0x0F, 0x12)`).  Our `set_channel()`
faithfully preserves whatever value is in the blob, but we've never
asked: does writing a different value actually change the audio path,
or is this purely a metadata label?

**Repro steps:**
1.  Connect.  Read all 8 channel-state blobs and write down the current
    `blob[253]` values.
2.  Start capture.
3.  In the GUI, change channel 1's "speaker role" or "type" dropdown
    (whichever the V1.24 GUI labels it) to several different values
    in sequence: e.g. FL_HIGH (1) → FL_MID (2) → CENTER (17) → SUB (18).
    Pause 1 s between each.
4.  Stop capture.

**What to look for:**
- Likely uses our existing per-channel write path
  (`cmd=0x1F0N` payload byte[7] is the subidx) — so the only thing this
  capture reveals is whether the GUI uses the same cmd or a different
  one.  If different, the cmd code + payload tell us how the role
  metadata is stored independently of the channel write.

**Validation plan:** loopback rig — same tone in, same routing, varying
spk_type → does the audio output change at all?  If yes, the byte is
load-affecting and we need a real API.  If no, it's purely a UI label.

---

## 7. Mixer cells with non-binary levels  (low value)

**What we don't know:** whether the GUI ever uses mixer cell values
other than 0 / 100.  Our live characterization
(`tests/loopback/test_routing_percentage.py` on the `loopback-rig`
branch) confirmed the firmware accepts the full 0..255 range with a
clean `20·log10(level/100)` dB curve and even allows boost above unity,
but we've never seen the *Windows GUI itself* write anything other than
0 / 100 — it appears to expose only on/off toggles.  This capture would
just confirm "no, the GUI is on/off only" or surface a hidden
per-cell-gain UI we missed.

**Repro steps:** if the V1.24 GUI has any mixer matrix screen with
faders or numeric values per cell, exercise them; otherwise this is a
no-op.  Worst-case: nothing to capture.

---

## 8. VU-meter / live level data path  (high value — blocks MQTT meters)

**What we don't know:** where the live VU-meter byte stream actually
comes from when the GUI's *Streaming* toggle is ON.  Empirical probing
on the `loopback-rig` branch (`tests/loopback/_probe_state13.py` and
`_probe_idle_poll.py`) tested the two obvious candidates and **both
failed**:

- `cmd=0x13` (10 bytes, currently exposed as `read_state_0x13()`) is
  **completely static**: every byte unchanged across -60 → 0 dBFS sweeps
  on DSP IN 1 *and* IN 2, with the routed output muted, with master
  muted.  Not meters.
- `cmd=0x03` (15 bytes, exposed as `idle_poll()`) is also completely
  static under the same sweep — last byte is always `0x01`, the other
  14 are zero whether audio is playing or not.

The existing `windows-04c-stream-nostream-stream.pcapng` shows the GUI
spamming `cmd=0x03` at ~30 Hz during streaming, but that capture was
taken with **no audio actually flowing through the device** — the GUI
was just toggling settings.  So the 14 leading zeros in the response
might be meters that simply happened to read 0, *or* meters live on a
totally different cmd / endpoint that the streaming toggle enables.

**Repro steps:**
1.  Wire a stereo audio source into Scarlett OUT 1+2 → DSP IN 1+2 (or
    use the GUI's own playback path if it has one).  *Audio must
    actually be flowing while the capture runs* — that's the whole
    point of this capture vs. the existing one.
2.  Start USBPcap.
3.  Click *Streaming → ON*.
4.  Play a tone or music for 5–10 s with audible level changes
    (start quiet, ramp up, mute, ramp again).  Note timestamps.
5.  Click *Streaming → OFF*.
6.  Stop the capture.

**What to look for in the pcapng:**
- During streaming-ON, are the device→host `cmd=0x03` 15-byte payloads
  *non-zero* and varying with the tone?  If yes, `cmd=0x03` IS the
  meter cmd and we need to repeat the loopback probe but emit the
  exact host→device `cmd=0x03 data="Custom"` write before each read
  (we tested only the plain READ form — maybe the device only emits
  meter values after the host's write nudges it).
- If `cmd=0x03` payloads are still zero: scan ALL interrupt-IN frames
  during the streaming window for *any* cmd whose payload bytes vary
  in time.  Likely candidates: `cmd=0x60`, anything else > 0x40 we
  haven't seen as a read.  Also check whether bulk or isochronous
  endpoints get traffic during streaming (the analyzer currently
  filters to interrupt — check raw `tshark` output).
- Cross-reference: does *any* bidirectional cmd start firing only when
  streaming toggles on?  That's our meter cmd.

**Validation plan once decoded:**
- Loopback rig — drive a known tone level into IN 1, poll the decoded
  meter cmd, fit a dB curve to the byte values.  Should also show
  byte-to-channel mapping (4 inputs + 8 outputs + 2 master / 14? other
  shape?).
- Then implement the MQTT live-meters feature with configurable
  `meter_poll_hz` (separate from the slow state poll), noise-floor
  threshold gating to avoid spamming the broker, and a binary
  (`has_audio`) vs continuous (`level_db`) per-channel mode toggle.

---

## 9. Bonus: catch the unknown `cmd=0x60 cat=0x09` in context

A single all-zero `cmd=0x60` write at `t≈10s` shows up in
`windows-04b-volumes-mute-presets.pcapng` and is otherwise unexplained.
It's near the start of the user session (after the connect handshake
finishes), so it could be a "session-ready" handshake step, "begin
preset polling," or even a "subscribe to push notifications" register.
Captures #2 (factory reset) and #3 (preset save/load) are likely to
include more `0x60` traffic in different states, which would
disambiguate.

---

## How to capture

USBPcap on Windows is what produced every existing `windows-*.pcapng`
under `captures/`.  Workflow that's worked well:

1.  Disconnect anything else on the same USB hub if possible (the
    Wireshark filter `usb.device_address == N` cuts out cross-talk but
    less is better).
2.  Run USBPcap, pick the DSP-408's USB device (VID `0483:5750`).
3.  Start capture *before* clicking anything in the GUI so we get the
    enumeration + handshake too — that gives a clean baseline.
4.  Do the steps for one item above, with deliberate ~1–2 s pauses
    between actions so the WRITE frames stand out in the timeline view.
5.  Stop, save as `captures/windows-NN-<short-description>.pcapng`,
    add a short `.txt` summary alongside (just header info — frame
    count, time span, what action happened when, like the existing
    `windows-04*.txt` files do).

A 30–60 s capture per item is plenty.  Smaller is better — easier to
diff against existing captures.

---

## Side note: blob field offsets may have shifted between firmware versions

`notes/blob-layout-verification.md` (from an earlier verification run)
puts the compressor record at offsets **278..285**:

```
allPassQ_le16  at 278..279  reads 420
attackTime     at 280..281  reads 56 ms
releaseTime    at 282..283  reads 500 ms
threshold      at 284
linkgroup      at 285
```

But `dsp408/protocol.py` currently has them at offsets **270..277**
(8 bytes earlier).  The new compressor cmd we just decoded
(`cmd=0x2300+ch`) writes the *same* values (Q=420, attack=56,
release=500), so those values clearly *are* there — but the offsets
should be cross-checked against a fresh `read_channel_state()` blob to
see which file is right for current firmware.  This isn't a capture
question per se, but it's a convenient thing to verify whenever the
rig is hot.
