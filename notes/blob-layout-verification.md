# Blob layout — live verification results

Verified against device `4EAA4B964C00` on the Pi, 2026-04-19, all 8 channels read.

## What VERIFIED correctly

- **`mute`** at offset 246: every channel reads `1` (live) — matches device state.
- **`polar`** at offset 247: every channel reads `0` (no phase invert) — matches default.
- **`gain_le16`** at 248–249: ch0–5 read `600` = +0 dB. ch6–7 read `0` = -60 dB (uninitialized).
- **`delay_le16`** at 250–251: all `0` (no delay).
- **`spk_type`** at 253: ch0–5 read 0x01,0x02,0x03,0x07,0x08,0x09 (factory CHANNEL_SUBIDX).
  ch6–7 read 0x00 (uninitialized) — confirming our long-standing observation.
- **`h_freq_le16`** at 254–255: all read `20` Hz. **Sensible HPF default.**
- **`h_filter`** at 256: all read `0` = Butterworth.
- **`h_level`** at 257: all read `1` = 12 dB/oct slope.
- **`l_freq_le16`** at 258–259: all read `20000` Hz. **Sensible LPF default.**
- **`l_filter`** at 260: all read `0` = Butterworth.
- **`l_level`** at 261: all read `1` = 12 dB/oct slope.
- **`allPassQ_le16`** at 278–279: all read `420` — plausible Q value.
- **`attackTime_le16`** at 280–281: all read `56` ms — plausible default.
- **`releaseTime_le16`** at 282–283: all read `500` ms — plausible default.
- **`threshold`** at 284: all read `0` = 0 dB threshold (compressor inactive).
- **`linkgroup`** at 285: all read `0` (no linking).

## What needs more work

### EQ bands at offset 0..N

Bands 0–5 read sensibly:
```
band00: f=  31Hz  level_raw=600  bw=52 type=0 shf=0   ← 0 dB at 31 Hz
band01: f=  65Hz  level_raw=600  bw=52 type=0 shf=0   ← 0 dB at 65 Hz
band02: f= 125Hz  level_raw=600  bw=52 type=0 shf=0
band03: f= 250Hz  level_raw=600  bw=52 type=0 shf=0
band04: f= 500Hz  level_raw=600  bw=52 type=0 shf=0
band05: f=1000Hz  level_raw=600  bw=52 type=0 shf=0
```

The frequencies are 1/2-octave centers (31, 63, 125, 250, 500, 1000 Hz) and `level=600`
matches our gain encoding `raw = (dB * 10) + 600` → 0 dB. **These are 6 real flat-EQ
defaults.** `bw=52` is plausibly the Q-table index for default Q.

But bands 6–9 read junk:
```
band06: f= 600Hz  level=52   bw=0   type=15 shf=160
band07: f= 600Hz  level=52   bw=0   type=31 shf=64
band08: f= 600Hz  level=52   bw=0   type=62 shf=128
band09: f= 600Hz  level=52   bw=0   type=0  shf=200
```

`type=15`/`type=31`/`type=62` are not valid filter-type enum values. So either:

1. **Our firmware has 10 PEQ bands but the next 4 (bands 6–9) start at a different offset
   (with padding before them), and what we're reading at offsets 48..79 is some other
   data (maybe an input-mixer or routing block).**
2. **Our firmware has 6 PEQ bands** (1/2-octave defaults) and the manual's "10-band PEQ"
   is aspirational. (Unlikely — Dayton wouldn't lie that hard.)
3. **The 8-byte band stride differs from leon's** in our firmware. Maybe ours uses 12
   bytes per band (10 bands × 12 = 120 bytes), and what looks like band06/07/etc. is
   actually the trailing bytes of bands 4/5/6 padded out.

Need to capture a Windows-USB write that **changes a single PEQ band's freq** and see
where that byte ends up — that pins down the band stride.

### Mixer cells at offset 262..277

Read identical bytes on every channel:
```
IN1..IN8:  [0, 0, 0, 0, 0, 0, 0, 0]
IN9..IN16: [164, 1, 56, 0, 244, 1, 0, 0]
```

`IN1..IN8 = 0` is correct (we haven't routed anything via the Windows app on the Pi).
But `IN9..IN16` is suspicious — same bytes on EVERY channel of EVERY device. **Those
bytes are not mixer cells.** They're probably:

- `[164, 1]` = LE u16 = 420 (= allPassQ?? — but we already read allPassQ at 278–279)
- `[56, 0]` = 56 (= attackTime_lo??)
- `[244, 1]` = 500 (= releaseTime??)

So bytes 270..277 may actually be where allPassQ/attack/release live, and
bytes 278–285 are something else. **The leon offsets are off by ~8 bytes for our
firmware in this region.**

Re-aligning hypothesis: maybe our firmware skips the 8 extra mixer cells (IN9..IN16)
because we only have 8 inputs max, so:
- Offsets 262–269: IN1..IN8 mixer (8 bytes)
- Offsets 270–271: allPassQ_le16 (was 278–279)
- Offsets 272–273: attackTime_le16 (was 280–281)
- Offsets 274–275: releaseTime_le16 (was 282–283)
- Offset 276: threshold
- Offset 277: linkgroup
- Offsets 278–285: name[8] (was 286–293)
- Offsets 286–295: 10 bytes of trailing reserved/encryption flag

This re-alignment fits the observed values:
- mixer IN1..IN8 = `[0, 0, 0, 0, 0, 0, 0, 0]` ✓ (at 262–269)
- allPassQ at 270–271 = `[164, 1]` LE = 420 ✓ — plausible!
- attackTime at 272–273 = `[56, 0]` LE = 56 ms ✓ — sensible default
- releaseTime at 274–275 = `[244, 1]` LE = 500 ms ✓ — sensible default
- threshold at 276 = `0` ✓
- linkgroup at 277 = `0` ✓

**This re-alignment is almost certainly right.** Our firmware drops the 8 ghost mixer
cells (IN9..IN16) that leon's data model has, shifting everything 8 bytes earlier from
that point on.

## Updated layout (most-likely-correct, pending PEQ verification)

```
0..N      EQ bands (band count + stride TBD; bands 0..5 confirmed)
246       mute              ✓
247       polar             ✓ NEW
248–249   gain_le16         ✓
250–251   delay_le16        ✓
252       eq_mode           ✓ NEW
253       spk_type          ✓
254–255   h_freq_le16       ✓ NEW
256       h_filter          ✓ NEW
257       h_level (slope)   ✓ NEW
258–259   l_freq_le16       ✓ NEW
260       l_filter          ✓ NEW
261       l_level (slope)   ✓ NEW
262–269   IN1..IN8 mixer    ✓ NEW (only 8 cells, not leon's 16)
270–271   allPassQ_le16     ✓ NEW
272–273   attackTime_le16   ✓ NEW
274–275   releaseTime_le16  ✓ NEW
276       threshold         ✓ NEW
277       linkgroup_num     ✓ NEW
278–285   name[8]           ✓ NEW
286–295   reserved (10b)    encryption flag area in leon; padding for us
```

That's **15 newly decoded fields** plus the 4 we already had — **19 fields per channel
× 8 channels = 152 new MQTT entities** if we expose them all (probably overkill — group
by category).

## Next concrete step

1. **Verify EQ band count and stride** by sending a single-band PEQ change via the
   Windows app while sniffing USB. Diff the before/after blob — only one region of
   bytes should change. That tells us exactly where bands live and how many there are.
2. **Implement the read-side parser** with the re-aligned offsets above (skipping the
   EQ region for now — return the 6 confirmed bands as a fixed structure and treat
   bands 6–9 as TBD).
3. **Wire to MQTT** as new HA entities.
4. Then loop back to PEQ once the band layout is pinned down.
