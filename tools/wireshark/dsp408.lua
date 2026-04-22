-- dsp408.lua — Wireshark dissector for the Dayton Audio DSP-408 USB HID protocol.
--
-- Install:  copy to ~/.config/wireshark/plugins/ (Linux/macOS) or
--           %APPDATA%\Wireshark\plugins\ (Windows), then Tools → Lua → Reload.
--
-- Mirrors dsp408/protocol.py from the dsp408-py library. See tools/wireshark/README.md.
--
-- v2: multi-frame reassembly for 296-byte full_channel_state writes and
-- read_channel_state / read_input_state reads. See README "Multi-frame
-- protocol deep dive" for the wire-level invariants.

local dsp408 = Proto("dsp408", "Dayton Audio DSP-408 HID")

-- ── Protocol constants (mirror dsp408/protocol.py) ─────────────────────
local FRAME_MAGIC = 0x808080EE
local END_MARKER  = 0xAA
local HEADER_SIZE = 14
local FRAME_SIZE  = 64
local MAX_PAYLOAD_SINGLE = FRAME_SIZE - HEADER_SIZE - 2   -- 48
local MAX_PAYLOAD_MULTI_FIRST = FRAME_SIZE - HEADER_SIZE  -- 50

-- Max continuation URBs we'll accept after a multi-frame first. Captures
-- observed: 4 continuations for 296-byte blobs. Set generously with a
-- safety margin for any future larger payloads — we bail out if exceeded.
local MAX_CONTINUATION_FRAMES = 12

local DIR_NAMES = {
  [0xA1] = "WRITE (host→dev)",
  [0xA2] = "READ request (host→dev)",
  [0x51] = "WRITE ack (dev→host)",
  [0x53] = "READ reply (dev→host)",
}
local DIR_SHORT = {
  [0xA1] = "WR", [0xA2] = "RD", [0x51] = "WR_ACK", [0x53] = "RD_RESP",
}

local CAT_NAMES = {
  [0x03] = "INPUT (music / input-side)",
  [0x04] = "PARAM (output parameter)",
  [0x09] = "STATE (system/global)",
}

local SLOPE_NAMES = {
  [0]="6 dB/oct", [1]="12 dB/oct", [2]="18 dB/oct", [3]="24 dB/oct",
  [4]="30 dB/oct", [5]="36 dB/oct", [6]="42 dB/oct", [7]="48 dB/oct",
  [8]="Off",
}
local FILTER_TYPE_NAMES = {
  [0]="Butterworth", [1]="Bessel", [2]="Linkwitz-Riley",
  [3]="Linkwitz-Riley (alias — GUI 'Defeat')",
}
local SPK_TYPE_NAMES = {
  [0]="none", [1]="fl_high", [2]="fl_mid", [3]="fl_low", [4]="fl",
  [5]="fr_high", [6]="fr_mid", [7]="fr_low", [8]="fr",
  [9]="rl_high", [10]="rl_mid", [11]="rl_low", [12]="rl",
  [13]="rr_high", [14]="rr_mid", [15]="rr_low", [16]="rr",
  [17]="center", [18]="sub", [19]="sub_l", [20]="sub_r",
  [21]="aux1", [22]="aux2", [23]="aux3", [24]="aux4",
}

-- ── Fields ────────────────────────────────────────────────────────────
local f = dsp408.fields
f.magic       = ProtoField.uint32("dsp408.magic",       "Magic",       base.HEX)
f.direction   = ProtoField.uint8 ("dsp408.direction",   "Direction",   base.HEX, DIR_NAMES)
f.version     = ProtoField.uint8 ("dsp408.version",     "Version",     base.HEX)
f.seq         = ProtoField.uint8 ("dsp408.seq",         "Sequence",    base.DEC)
f.category    = ProtoField.uint8 ("dsp408.category",    "Category",    base.HEX, CAT_NAMES)
f.cmd         = ProtoField.uint32("dsp408.cmd",         "Command",     base.HEX)
f.cmd_name    = ProtoField.string("dsp408.cmd_name",    "Command name")
f.payload_len = ProtoField.uint16("dsp408.payload_len", "Payload length (declared)", base.DEC)
f.payload     = ProtoField.bytes ("dsp408.payload",     "Payload bytes (this frame)")
f.checksum    = ProtoField.uint8 ("dsp408.checksum",    "XOR checksum", base.HEX)
f.checksum_ok = ProtoField.bool  ("dsp408.checksum_ok", "Checksum valid")
f.end_marker  = ProtoField.uint8 ("dsp408.end_marker",  "End marker",  base.HEX)
f.multiframe  = ProtoField.bool  ("dsp408.multiframe",  "Multi-frame payload")

-- Continuation-frame fields
f.cont_of         = ProtoField.uint32("dsp408.continuation_of",    "Continuation of frame", base.DEC)
f.cont_index      = ProtoField.uint8 ("dsp408.continuation_index", "Continuation N of M",   base.DEC)
f.cont_total      = ProtoField.uint8 ("dsp408.continuation_total", "Total continuations",   base.DEC)
f.reassembled_len = ProtoField.uint16("dsp408.reassembled_len",    "Reassembled payload length", base.DEC)
f.reassembled     = ProtoField.bytes ("dsp408.reassembled",        "Reassembled payload")

-- Decoded fields (shared across cmds — Wireshark doesn't care if some never fire)
f.d_channel   = ProtoField.uint8 ("dsp408.channel",     "Channel (0-based)",    base.DEC)
f.d_band      = ProtoField.uint8 ("dsp408.eq_band",     "EQ band (0..9)",       base.DEC)

-- master
f.m_level_raw = ProtoField.uint8 ("dsp408.master.level_raw", "Master level raw", base.DEC)
f.m_level_db  = ProtoField.int8  ("dsp408.master.level_db",  "Master level (dB)", base.DEC)
f.m_mute      = ProtoField.bool  ("dsp408.master.mute",      "Muted")

-- channel write
f.c_enable    = ProtoField.bool  ("dsp408.channel.enable",   "Enabled (audible)")
f.c_vol_raw   = ProtoField.uint16("dsp408.channel.vol_raw",  "Volume raw", base.DEC)
f.c_vol_db    = ProtoField.float ("dsp408.channel.vol_db",   "Volume (dB)")
f.c_delay     = ProtoField.uint16("dsp408.channel.delay",    "Delay (samples)", base.DEC)
f.c_subidx    = ProtoField.uint8 ("dsp408.channel.subidx",   "Subindex / spk_type", base.HEX)

-- routing
f.r_in1       = ProtoField.uint8 ("dsp408.routing.in1", "IN1 level", base.HEX)
f.r_in2       = ProtoField.uint8 ("dsp408.routing.in2", "IN2 level", base.HEX)
f.r_in3       = ProtoField.uint8 ("dsp408.routing.in3", "IN3 level", base.HEX)
f.r_in4       = ProtoField.uint8 ("dsp408.routing.in4", "IN4 level", base.HEX)
f.r_in5       = ProtoField.uint8 ("dsp408.routing.in5", "IN5 level", base.HEX)
f.r_in6       = ProtoField.uint8 ("dsp408.routing.in6", "IN6 level", base.HEX)
f.r_in7       = ProtoField.uint8 ("dsp408.routing.in7", "IN7 level", base.HEX)
f.r_in8       = ProtoField.uint8 ("dsp408.routing.in8", "IN8 level", base.HEX)

-- crossover
f.x_hpf_freq   = ProtoField.uint16("dsp408.xover.hpf_freq",   "HPF freq (Hz)", base.DEC)
f.x_hpf_type   = ProtoField.uint8 ("dsp408.xover.hpf_type",   "HPF filter", base.DEC, FILTER_TYPE_NAMES)
f.x_hpf_slope  = ProtoField.uint8 ("dsp408.xover.hpf_slope",  "HPF slope",  base.DEC, SLOPE_NAMES)
f.x_lpf_freq   = ProtoField.uint16("dsp408.xover.lpf_freq",   "LPF freq (Hz)", base.DEC)
f.x_lpf_type   = ProtoField.uint8 ("dsp408.xover.lpf_type",   "LPF filter", base.DEC, FILTER_TYPE_NAMES)
f.x_lpf_slope  = ProtoField.uint8 ("dsp408.xover.lpf_slope",  "LPF slope",  base.DEC, SLOPE_NAMES)

-- eq band
f.e_freq      = ProtoField.uint16("dsp408.eq.freq",     "Centre freq (Hz)", base.DEC)
f.e_gain_raw  = ProtoField.uint16("dsp408.eq.gain_raw", "Gain raw", base.DEC)
f.e_gain_db   = ProtoField.float ("dsp408.eq.gain_db",  "Gain (dB)")
f.e_bw        = ProtoField.uint8 ("dsp408.eq.bw",       "Bandwidth byte", base.DEC)
f.e_q         = ProtoField.float ("dsp408.eq.q",        "Q (≈256/bw)")

-- compressor
f.k_q         = ProtoField.uint16("dsp408.comp.all_pass_q", "All-pass Q",  base.DEC)
f.k_attack    = ProtoField.uint16("dsp408.comp.attack_ms",  "Attack (ms)", base.DEC)
f.k_release   = ProtoField.uint16("dsp408.comp.release_ms", "Release (ms)", base.DEC)
f.k_threshold = ProtoField.uint8 ("dsp408.comp.threshold",  "Threshold",   base.DEC)
f.k_linkgroup = ProtoField.uint8 ("dsp408.comp.linkgroup",  "Link group",  base.DEC)

-- name
f.n_name      = ProtoField.string("dsp408.name", "Channel name")

-- factory reset
f.fr_magic    = ProtoField.string("dsp408.factory_reset.magic", "Factory-reset magic")

-- preset save
f.ps_byte     = ProtoField.uint8 ("dsp408.preset_save.byte",   "Preset-save trigger byte", base.HEX)

-- Expert info
local ef_bad_magic    = ProtoExpert.new("dsp408.bad_magic",    "Bad magic",
                                        expert.group.MALFORMED, expert.severity.ERROR)
local ef_bad_checksum = ProtoExpert.new("dsp408.bad_checksum", "Bad XOR checksum",
                                        expert.group.CHECKSUM, expert.severity.ERROR)
local ef_bad_end      = ProtoExpert.new("dsp408.bad_end",      "Missing end marker 0xAA",
                                        expert.group.MALFORMED, expert.severity.WARN)
local ef_multiframe   = ProtoExpert.new("dsp408.multiframe",   "Multi-frame payload — reassembly in progress",
                                        expert.group.COMMENTS_GROUP, expert.severity.NOTE)
local ef_abandoned    = ProtoExpert.new("dsp408.abandoned",    "Abandoned multi-frame (new magic before completion)",
                                        expert.group.MALFORMED, expert.severity.WARN)
local ef_reassembled  = ProtoExpert.new("dsp408.reassembled",  "Multi-frame reassembled here",
                                        expert.group.COMMENTS_GROUP, expert.severity.NOTE)
dsp408.experts = { ef_bad_magic, ef_bad_checksum, ef_bad_end, ef_multiframe,
                   ef_abandoned, ef_reassembled }

-- ── Command name resolution ────────────────────────────────────────────
local function resolve_cmd(cmd, direction, category, payload_len)
  if category == 0x09 then
    if cmd == 0x00 then return "preset_name", "preset_name" end
    if cmd == 0x02 then return "global_0x02", nil end
    if cmd == 0x03 then return "idle_poll",   nil end
    if cmd == 0x04 then
      if direction == 0xA1 and payload_len == 296 then
        return "full_channel_state(ch=4)", "full_channel_state"
      end
      return "get_info", nil
    end
    if cmd >= 0x05 and cmd <= 0x07 then
      if direction == 0xA1 and payload_len == 296 then
        return string.format("full_channel_state(ch=%d)", cmd), "full_channel_state"
      end
      if cmd == 0x05 then return "master", "master" end
      if cmd == 0x06 then return "global_0x06", nil end
      if cmd == 0x07 then return string.format("cmd_0x%02X", cmd), nil end
    end
    if cmd == 0x13 then return "state_0x13", nil end
    if cmd == 0x34 then
      if direction == 0xA1 then return "preset_save_trigger", "preset_save" end
      return "status", nil
    end
    if cmd == 0x36 then return "fw_prep",  nil end
    if cmd == 0x37 then return "fw_meta",  nil end
    if cmd == 0x38 then return "fw_block", nil end
    if cmd == 0x39 then return "fw_apply", nil end
    if cmd == 0xCC then return "connect",  nil end
  end

  if category == 0x03 then
    local hi = bit.rshift(cmd, 8)
    local lo = bit.band(cmd, 0xFF)
    if hi == 0x77 then return string.format("read_input_state(ch=%d)", lo), "read_input_state" end
    if hi == 0x09 then return string.format("input_misc(ch=%d)", lo), nil end
    if hi == 0x0A then return string.format("input_dataid10(ch=%d)", lo), nil end
    if hi == 0x0B then return string.format("input_noisegate(ch=%d)", lo), nil end
    if hi >= 0x00 and hi <= 0x0E then
      return string.format("input_eq_band(band=%d,ch=%d)", hi, lo), nil
    end
  end

  if category == 0x04 then
    local lo = bit.band(cmd, 0xFF)
    if cmd >= 0x7700 and cmd <= 0x77FF then
      if direction == 0x53 and payload_len == 296 then
        return string.format("read_channel_state(ch=%d)", lo), "read_channel_state"
      end
      return string.format("read_channel_state(ch=%d)", lo), nil
    end
    if cmd >= 0x1F00 and cmd <= 0x1F07 then
      return string.format("channel(ch=%d)", lo), "channel"
    end
    if cmd == 0x2000 then
      return "write_global / factory_reset", "factory_reset"
    end
    if cmd >= 0x2100 and cmd <= 0x2107 then
      return string.format("routing(ch=%d)", lo), "routing"
    end
    if cmd >= 0x2200 and cmd <= 0x2207 then
      return string.format("routing_hi(ch=%d)", lo), "routing_hi"
    end
    if cmd >= 0x2300 and cmd <= 0x2307 then
      return string.format("compressor(ch=%d)", lo), "compressor"
    end
    if cmd >= 0x2400 and cmd <= 0x2407 then
      return string.format("channel_name(ch=%d)", lo), "channel_name"
    end
    if cmd >= 0x10000 and cmd <= 0x10FFF then
      local band = bit.band(bit.rshift(cmd, 8), 0x0F)
      local ch   = bit.band(cmd, 0xFF)
      if direction == 0xA1 and payload_len == 296 and band == 0 then
        return string.format("full_channel_state(ch=%d)", ch), "full_channel_state"
      end
      return string.format("eq_band(band=%d,ch=%d)", band, ch), "eq_band"
    end
    if cmd >= 0x12000 and cmd <= 0x12007 then
      return string.format("crossover(ch=%d)", bit.band(cmd, 0xFF)), "crossover"
    end
  end

  return string.format("cmd_0x%X", cmd), nil
end

-- ── Payload decoders (single-frame 8-byte payloads) ───────────────────

local function decode_master(tvb, tree)
  if tvb:len() < 8 then return nil end
  local raw  = tvb(0,1):uint()
  local mute = tvb(6,1):uint() == 0
  local db   = raw - 60
  tree:add(f.m_level_raw, tvb(0,1)):append_text(string.format(" (%+d dB)", db))
  tree:add(f.m_level_db,  tvb(0,1), db)
  tree:add(f.m_mute,      tvb(6,1), mute)
  return string.format("level=%+d dB%s", db, mute and " MUTED" or "")
end

local function decode_channel(tvb, tree, cmd)
  if tvb:len() < 8 then return nil end
  local ch     = bit.band(cmd, 0xFF)
  local enable = tvb(0,1):uint() ~= 0
  local vol    = tvb(2,2):le_uint()
  local delay  = tvb(4,2):le_uint()
  local subidx = tvb(7,1):uint()
  local vol_db = (vol - 600) / 10
  tree:add(f.d_channel, ch)
  tree:add(f.c_enable,  tvb(0,1), enable)
  tree:add(f.c_vol_raw, tvb(2,2), vol):append_text(string.format(" (%+0.1f dB)", vol_db))
  tree:add(f.c_vol_db,  tvb(2,2), vol_db)
  tree:add(f.c_delay,   tvb(4,2), delay)
  local sub_name = SPK_TYPE_NAMES[subidx] or "?"
  tree:add(f.c_subidx,  tvb(7,1), subidx):append_text(" ("..sub_name..")")
  return string.format("ch=%d %s vol=%+0.1fdB delay=%d", ch,
                       enable and "ON" or "MUTE", vol_db, delay)
end

local function decode_routing(tvb, tree, cmd, hi)
  if tvb:len() < 8 then return nil end
  local ch = bit.band(cmd, 0xFF)
  local cells = {}
  local fields = { f.r_in1, f.r_in2, f.r_in3, f.r_in4, f.r_in5, f.r_in6, f.r_in7, f.r_in8 }
  local labels = hi
    and { "IN9","IN10","IN11","IN12","IN13","IN14","IN15","IN16" }
    or  { "IN1","IN2","IN3","IN4","IN5","IN6","IN7","IN8" }
  tree:add(f.d_channel, ch)
  for i = 0, 7 do
    local v = tvb(i, 1):uint()
    tree:add(fields[i+1], tvb(i,1)):prepend_text(labels[i+1].." = ")
    if v ~= 0 then cells[#cells+1] = string.format("%s=0x%02X", labels[i+1], v) end
  end
  if #cells == 0 then
    return string.format("ch=%d (all off)", ch)
  end
  return string.format("ch=%d %s", ch, table.concat(cells, ","))
end

local function decode_crossover(tvb, tree, cmd)
  if tvb:len() < 8 then return nil end
  local ch = bit.band(cmd, 0xFF)
  local hpf_f = tvb(0,2):le_uint()
  local hpf_t = tvb(2,1):uint()
  local hpf_s = tvb(3,1):uint()
  local lpf_f = tvb(4,2):le_uint()
  local lpf_t = tvb(6,1):uint()
  local lpf_s = tvb(7,1):uint()
  tree:add(f.d_channel, ch)
  tree:add_le(f.x_hpf_freq, tvb(0,2))
  tree:add(f.x_hpf_type, tvb(2,1))
  tree:add(f.x_hpf_slope, tvb(3,1))
  tree:add_le(f.x_lpf_freq, tvb(4,2))
  tree:add(f.x_lpf_type, tvb(6,1))
  tree:add(f.x_lpf_slope, tvb(7,1))
  return string.format("ch=%d HPF=%dHz/%s/%s LPF=%dHz/%s/%s",
    ch, hpf_f, FILTER_TYPE_NAMES[hpf_t] or "?", SLOPE_NAMES[hpf_s] or "?",
        lpf_f, FILTER_TYPE_NAMES[lpf_t] or "?", SLOPE_NAMES[lpf_s] or "?")
end

local function decode_eq_band(tvb, tree, cmd)
  if tvb:len() < 8 then return nil end
  local band = bit.band(bit.rshift(cmd, 8), 0x0F)
  local ch   = bit.band(cmd, 0xFF)
  local freq = tvb(0,2):le_uint()
  local gain = tvb(2,2):le_uint()
  local bw   = tvb(4,1):uint()
  local gain_db = (gain - 600) / 10
  local q    = bw > 0 and (256.0 / bw) or 0
  tree:add(f.d_band, band)
  tree:add(f.d_channel, ch)
  tree:add_le(f.e_freq, tvb(0,2))
  tree:add_le(f.e_gain_raw, tvb(2,2)):append_text(string.format(" (%+0.1f dB)", gain_db))
  tree:add_le(f.e_gain_db, tvb(2,2), gain_db)
  tree:add(f.e_bw, tvb(4,1)):append_text(string.format(" (Q≈%0.2f)", q))
  tree:add(f.e_q, tvb(4,1), q)
  return string.format("band=%d ch=%d f=%dHz gain=%+0.1fdB Q≈%0.2f",
                       band, ch, freq, gain_db, q)
end

local function decode_compressor(tvb, tree, cmd)
  if tvb:len() < 8 then return nil end
  local ch = bit.band(cmd, 0xFF)
  local q       = tvb(0,2):le_uint()
  local attack  = tvb(2,2):le_uint()
  local release = tvb(4,2):le_uint()
  local thresh  = tvb(6,1):uint()
  local link    = tvb(7,1):uint()
  tree:add(f.d_channel, ch)
  tree:add_le(f.k_q, tvb(0,2))
  tree:add_le(f.k_attack, tvb(2,2))
  tree:add_le(f.k_release, tvb(4,2))
  tree:add(f.k_threshold, tvb(6,1))
  tree:add(f.k_linkgroup, tvb(7,1))
  return string.format("ch=%d Q=%d attack=%dms release=%dms thresh=%d link=%d",
                       ch, q, attack, release, thresh, link)
end

local function decode_channel_name(tvb, tree, cmd)
  if tvb:len() < 1 then return nil end
  local ch = bit.band(cmd, 0xFF)
  local name = tvb:raw(0, math.min(8, tvb:len()))
  name = name:gsub("[%z%s]+$", "")
  tree:add(f.d_channel, ch)
  tree:add(f.n_name, tvb(0, math.min(8, tvb:len())), name)
  return string.format("ch=%d name=%q", ch, name)
end

local function decode_factory_reset(tvb, tree)
  if tvb:len() < 8 then return nil end
  local bytes = { tvb(0,1):uint(), tvb(1,1):uint(), tvb(2,1):uint(), tvb(3,1):uint(),
                  tvb(4,1):uint(), tvb(5,1):uint(), tvb(6,1):uint(), tvb(7,1):uint() }
  local is_reset = bytes[1]==0x06 and bytes[2]==0x1F and bytes[3]==0x00 and bytes[4]==0x00
               and bytes[5]==0x20 and bytes[6]==0x4E and bytes[7]==0x00 and bytes[8]==0x01
  if is_reset then
    tree:add(f.fr_magic, tvb(0,8), "FACTORY RESET (matches 06 1f 00 00 20 4e 00 01)")
    return "FACTORY RESET MAGIC"
  end
  return nil
end

local function decode_preset_save(tvb, tree)
  if tvb:len() < 1 then return nil end
  local b = tvb(0,1):uint()
  tree:add(f.ps_byte, tvb(0,1))
  if b == 0x01 then return "SAVE preset → flash" end
  return string.format("byte=0x%02X", b)
end

-- ── 296-byte channel-state blob decoder ──────────────────────────────
-- Offsets mirror dsp408/protocol.py BLOB layout (OFF_MUTE = 246, etc.).

local function decode_full_channel_state_blob(tvb, tree, cmd)
  -- tvb is the reassembled 296-byte payload
  if tvb:len() < 296 then
    return string.format("(partial blob, %d/296 bytes)", tvb:len())
  end
  local ch = bit.band(cmd, 0xFF)
  if cmd >= 0x10000 then ch = bit.band(cmd, 0x03) end
  tree:add(f.d_channel, ch)

  -- EQ bands: 10 × 8 bytes at offsets 0..79
  local eqtree = tree:add(tvb(0, 80), "EQ bands (10 × 8 bytes)")
  for band = 0, 9 do
    local off = band * 8
    local freq = tvb(off,   2):le_uint()
    local gain = tvb(off+2, 2):le_uint()
    local bw   = tvb(off+4, 1):uint()
    local gdb  = (gain - 600) / 10
    local q    = bw > 0 and (256.0 / bw) or 0
    eqtree:add(tvb(off, 8), string.format(
      "band %d: f=%d Hz gain=%+0.1f dB (raw=%d) bw=%d (Q≈%0.2f)",
      band, freq, gdb, gain, bw, q))
  end

  -- Basic record at 246..253: mute, polar, gain, delay, byte_252, spk_type
  local mute   = tvb(246,1):uint() == 0   -- INVERTED
  local polar  = tvb(247,1):uint()
  local vol    = tvb(248,2):le_uint()
  local delay  = tvb(250,2):le_uint()
  local spktyp = tvb(253,1):uint()
  local vol_db = (vol - 600) / 10
  local basic = tree:add(tvb(246, 8), "Basic (246..253)")
  basic:add(tvb(246,1), string.format("mute (inv): %s", mute and "MUTED" or "audible"))
  basic:add(tvb(247,1), string.format("polar: %s", polar == 0 and "normal" or "inverted"))
  basic:add(tvb(248,2), string.format("vol: %+0.1f dB (raw=%d)", vol_db, vol))
  basic:add(tvb(250,2), string.format("delay: %d samples", delay))
  basic:add(tvb(252,1), string.format("byte_252: 0x%02X (semantics unknown)", tvb(252,1):uint()))
  basic:add(tvb(253,1), string.format("spk_type: %d (%s)", spktyp, SPK_TYPE_NAMES[spktyp] or "?"))

  -- Crossover 254..261
  local hpf_f = tvb(254,2):le_uint()
  local hpf_t = tvb(256,1):uint()
  local hpf_s = tvb(257,1):uint()
  local lpf_f = tvb(258,2):le_uint()
  local lpf_t = tvb(260,1):uint()
  local lpf_s = tvb(261,1):uint()
  local xtree = tree:add(tvb(254, 8), "Crossover (254..261)")
  xtree:add(tvb(254,2), string.format("HPF: %d Hz", hpf_f))
  xtree:add(tvb(256,1), string.format("HPF type: %s", FILTER_TYPE_NAMES[hpf_t] or "?"))
  xtree:add(tvb(257,1), string.format("HPF slope: %s", SLOPE_NAMES[hpf_s] or "?"))
  xtree:add(tvb(258,2), string.format("LPF: %d Hz", lpf_f))
  xtree:add(tvb(260,1), string.format("LPF type: %s", FILTER_TYPE_NAMES[lpf_t] or "?"))
  xtree:add(tvb(261,1), string.format("LPF slope: %s", SLOPE_NAMES[lpf_s] or "?"))

  -- Mixer 262..269 (IN1..IN8 levels)
  local mxtree = tree:add(tvb(262, 8), "Mixer (262..269)")
  local mx = {}
  for i = 0, 7 do
    local v = tvb(262 + i, 1):uint()
    mxtree:add(tvb(262+i,1), string.format("IN%d: 0x%02X", i+1, v))
    if v ~= 0 then mx[#mx+1] = string.format("IN%d=0x%02X", i+1, v) end
  end

  -- 270..277: compressor shadow (read-only mirror, never changes) — skip detailed decode

  -- Compressor at 278..285
  local cq      = tvb(278,2):le_uint()
  local cattack = tvb(280,2):le_uint()
  local crel    = tvb(282,2):le_uint()
  local cthresh = tvb(284,1):uint()
  local clink   = tvb(285,1):uint()
  local ctree = tree:add(tvb(278, 8), "Compressor (278..285)")
  ctree:add(tvb(278,2), string.format("all_pass_q: %d", cq))
  ctree:add(tvb(280,2), string.format("attack: %d ms", cattack))
  ctree:add(tvb(282,2), string.format("release: %d ms", crel))
  ctree:add(tvb(284,1), string.format("threshold: %d", cthresh))
  ctree:add(tvb(285,1), string.format("linkgroup: %d", clink))

  -- Name at 286..293
  local name = tvb:raw(286, 8):gsub("[%z%s]+$", "")
  tree:add(f.n_name, tvb(286, 8), name)

  return string.format(
    "ch=%d %s vol=%+0.1fdB HPF=%dHz LPF=%dHz mixer=[%s] name=%q",
    ch, mute and "MUTED" or "ON", vol_db, hpf_f, lpf_f,
    #mx > 0 and table.concat(mx, ",") or "off",
    name)
end

local DECODERS = {
  ["master"]              = function(t, tr, c) return decode_master(t, tr) end,
  ["channel"]             = decode_channel,
  ["routing"]             = function(t, tr, c) return decode_routing(t, tr, c, false) end,
  ["routing_hi"]          = function(t, tr, c) return decode_routing(t, tr, c, true) end,
  ["crossover"]           = decode_crossover,
  ["eq_band"]             = decode_eq_band,
  ["compressor"]          = decode_compressor,
  ["channel_name"]        = decode_channel_name,
  ["factory_reset"]       = function(t, tr, c) return decode_factory_reset(t, tr) end,
  ["preset_save"]         = function(t, tr, c) return decode_preset_save(t, tr) end,
}

-- ── Multi-frame state tracking ────────────────────────────────────────
-- Multi-frame invariants (verified across 6 captures):
--   * First frame: 4-byte magic + 10-byte header + 50 payload bytes (no chk/end).
--   * payload_len in header declares the FULL logical payload size.
--   * N continuations = ceil((declared_len - 50) / 64), all 64-byte raw on the
--     SAME USB endpoint, no framing, no magic.
--   * Last continuation places chk + 0xAA at HID offset
--     (declared_len - 50 - 64*(N-1)) followed by zero padding.
--   * Device acks (WRITE) or host proceeds (READ) only after all bytes.
--   * No pre/post protocol herald — payload_len alone signals multi-frame.
--
-- State lives in two globals:
--   pending[conv_key]  — "there's a multi-frame in progress on this endpoint"
--   per_frame[fnum]    — per-packet memo for re-dissection (Wireshark
--                        visits packets multiple times when filtering).

local pending   = {}  -- conv_key → pending entry
local per_frame = {}  -- frame_number → frame info

-- Field extractor for USB endpoint (populated by the built-in USB dissector)
local f_usb_endpoint = Field.new("usb.endpoint_address")
local f_usb_src      = Field.new("usb.src")
local f_usb_dst      = Field.new("usb.dst")

local function conversation_key(pinfo)
  -- Combine endpoint + src/dst. Endpoint alone isn't enough if multiple
  -- DSP-408 devices are plugged (rare but real).
  local ep_finfo = f_usb_endpoint()
  local ep = ep_finfo and ep_finfo.value or 0
  local src, dst = tostring(pinfo.src), tostring(pinfo.dst)
  return string.format("%s>%s/%d", src, dst, ep)
end

local function expected_continuation_count(declared_len)
  if declared_len <= MAX_PAYLOAD_MULTI_FIRST then return 0 end
  return math.ceil((declared_len - MAX_PAYLOAD_MULTI_FIRST) / FRAME_SIZE)
end

-- ── Checksum helper ────────────────────────────────────────────────────
local function xor_checksum_range(buffer, start, stop_exclusive)
  local c = 0
  for i = start, stop_exclusive - 1 do
    c = bit.bxor(c, buffer(i, 1):uint())
  end
  return bit.band(c, 0xFF)
end

local function xor_checksum_bytes(bytes)
  local c = 0
  for i = 1, #bytes do
    c = bit.bxor(c, string.byte(bytes, i))
  end
  return bit.band(c, 0xFF)
end

-- ── Dissection: first / single frame ──────────────────────────────────

local function dissect_first_or_single(buffer, pinfo, tree)
  local len = buffer:len()
  pinfo.cols.protocol = "DSP-408"
  local subtree = tree:add(dsp408, buffer(), "Dayton DSP-408 HID frame")
  subtree:add(f.magic, buffer(0, 4))

  local direction   = buffer(4, 1):uint()
  local version     = buffer(5, 1):uint()
  local seq         = buffer(6, 1):uint()
  local category    = buffer(7, 1):uint()
  local cmd         = buffer(8, 4):le_uint()
  local payload_len = buffer(12, 2):le_uint()

  subtree:add(f.direction,   buffer(4, 1))
  subtree:add(f.version,     buffer(5, 1))
  subtree:add(f.seq,         buffer(6, 1))
  subtree:add(f.category,    buffer(7, 1))
  subtree:add_le(f.cmd,      buffer(8, 4))
  subtree:add_le(f.payload_len, buffer(12, 2))

  local name, dec_key = resolve_cmd(cmd, direction, category, payload_len)
  subtree:add(f.cmd_name, name)

  local is_multi = payload_len > MAX_PAYLOAD_SINGLE
  subtree:add(f.multiframe, is_multi):set_generated()

  local summary = nil
  local n_cont = 0
  local first_frame_info = per_frame[pinfo.number]

  if is_multi then
    n_cont = expected_continuation_count(payload_len)
    subtree:add_tvb_expert_info(ef_multiframe, buffer(12, 2),
      string.format("Multi-frame: declared %d bytes; expect %d continuation URB(s)",
                    payload_len, n_cont))

    -- First 50 payload bytes + register pending state (first pass only)
    local present = math.min(payload_len, MAX_PAYLOAD_MULTI_FIRST, len - HEADER_SIZE)
    local ptvb = buffer(HEADER_SIZE, present)
    subtree:add(f.payload, ptvb):append_text(" (first 50 bytes of multi-frame)")

    if not pinfo.visited then
      local key = conversation_key(pinfo)
      -- If a prior multi-frame is still pending on this conversation, mark
      -- it abandoned (new magic arrived before completion).
      local prior = pending[key]
      if prior then
        per_frame[prior.first_frame] =
          per_frame[prior.first_frame] or {}
        per_frame[prior.first_frame].abandoned = true
        pending[key] = nil
      end
      pending[key] = {
        first_frame = pinfo.number,
        cmd         = cmd,
        seq         = seq,
        category    = category,
        direction   = direction,
        declared    = payload_len,
        expected    = n_cont,
        received    = 0,
        accum       = { buffer:raw(HEADER_SIZE, present) },
        dec_key     = dec_key,
        name        = name,
      }
      per_frame[pinfo.number] = {
        role     = "first",
        expected = n_cont,
        name     = name,
        abandoned = false,
      }
    end
  else
    -- Single-frame path: decode payload normally
    local present = math.min(payload_len, MAX_PAYLOAD_SINGLE, len - HEADER_SIZE - 2)
    if present < 0 then present = 0 end
    if present > 0 then
      local ptvb = buffer(HEADER_SIZE, present)
      local ptree = subtree:add(f.payload, ptvb)
      if dec_key and DECODERS[dec_key] then
        local ok, s = pcall(DECODERS[dec_key], ptvb, ptree, cmd)
        if ok then summary = s end
      end
    end

    -- Checksum + end marker
    local chk_pos = HEADER_SIZE + present
    if chk_pos < len then
      local chk = buffer(chk_pos, 1):uint()
      local computed = xor_checksum_range(buffer, 4, chk_pos)
      local chk_item = subtree:add(f.checksum, buffer(chk_pos, 1))
      local ok = (chk == computed)
      subtree:add(f.checksum_ok, buffer(chk_pos, 1), ok):set_generated()
      if ok then
        chk_item:append_text(" (valid)")
      else
        chk_item:append_text(string.format(" (INVALID, expected 0x%02X)", computed))
        chk_item:add_tvb_expert_info(ef_bad_checksum, buffer(chk_pos, 1),
          string.format("XOR mismatch: got 0x%02X, expected 0x%02X", chk, computed))
      end
      if chk_pos + 1 < len then
        local em = buffer(chk_pos + 1, 1):uint()
        local em_item = subtree:add(f.end_marker, buffer(chk_pos + 1, 1))
        if em ~= END_MARKER then
          em_item:append_text(string.format(" (expected 0x%02X)", END_MARKER))
          em_item:add_tvb_expert_info(ef_bad_end, buffer(chk_pos + 1, 1))
        end
      end
    end
  end

  -- Info column
  local dir = DIR_SHORT[direction] or string.format("dir=0x%02X", direction)
  local info = string.format("%s seq=%d %s", dir, seq, name)
  if summary then info = info .. " " .. summary end
  if is_multi then
    info = info .. string.format(" [multi-frame first +%d cont]", n_cont)
    if first_frame_info and first_frame_info.abandoned then
      info = info .. " ABANDONED"
    end
  end
  pinfo.cols.info = info

  return len
end

-- ── Dissection: continuation frame ────────────────────────────────────

local function dissect_continuation(buffer, pinfo, tree)
  local key = conversation_key(pinfo)
  local state = pending[key]
  local info = per_frame[pinfo.number]

  pinfo.cols.protocol = "DSP-408"
  local subtree = tree:add(dsp408, buffer(), "Dayton DSP-408 HID continuation frame")

  -- First-pass consumption
  if not pinfo.visited and state then
    state.received = state.received + 1
    local cont_idx = state.received
    local is_last = (cont_idx == state.expected)
    -- How many payload bytes does this frame carry?
    local remaining = state.declared - MAX_PAYLOAD_MULTI_FIRST
                    - (cont_idx - 1) * FRAME_SIZE
    local payload_here = math.min(remaining, FRAME_SIZE)
    if payload_here < 0 then payload_here = 0 end
    state.accum[#state.accum + 1] = buffer:raw(0, payload_here)

    info = {
      role        = "continuation",
      first_frame = state.first_frame,
      index       = cont_idx,
      total       = state.expected,
      is_last     = is_last,
      payload_here = payload_here,
      cmd         = state.cmd,
      name        = state.name,
      dec_key     = state.dec_key,
    }
    per_frame[pinfo.number] = info

    if is_last then
      -- Validate chk + end marker at deterministic offset
      local chk_off = payload_here
      if chk_off + 1 < buffer:len() then
        local chk = buffer(chk_off, 1):uint()
        local em  = buffer(chk_off + 1, 1):uint()
        -- Compute expected chk: XOR of (header[4..13] of the first frame) + full payload.
        -- We don't carry the header bytes separately in accum, so rebuild.
        local full_payload = table.concat(state.accum)
        -- The first frame's header bytes 4..13 (dir, ver, seq, cat, cmd4, len2)
        -- are available only from the first frame. We didn't save them; the
        -- device-side XOR also includes those bytes. Skip strict validation
        -- but record the values for display.
        info.checksum    = chk
        info.end_marker  = em
        info.full_payload = full_payload
      end
      -- Reassembly complete — clear pending state on this conversation
      pending[key] = nil
    end
  end

  -- Dissect view (happens every pass)
  if info then
    subtree:add(f.cont_of,     info.first_frame):set_generated()
    subtree:add(f.cont_index,  info.index):set_generated()
    subtree:add(f.cont_total,  info.total):set_generated()

    local dir_hint = ""  -- we don't know direction from bytes alone
    local label = string.format("continuation %d/%d of frame %d (%s)",
                                info.index, info.total, info.first_frame,
                                info.name or "?")
    pinfo.cols.info = label

    if info.is_last then
      -- Add reassembled payload + decoded blob
      if info.full_payload then
        local full_tvb = ByteArray.new(info.full_payload, true):tvb("DSP-408 reassembled payload")
        subtree:add(f.reassembled_len, #info.full_payload):set_generated()
        local rtree = subtree:add(f.reassembled, full_tvb()):set_generated()
        rtree:add_tvb_expert_info(ef_reassembled, full_tvb(),
          string.format("Reassembled %d bytes from frames %d..%d",
                        #info.full_payload, info.first_frame, pinfo.number))

        -- Decode the 296-byte blob if cmd is full_channel_state / read_channel_state
        local summary = nil
        if info.dec_key == "full_channel_state" or info.dec_key == "read_channel_state" then
          local ok, s = pcall(decode_full_channel_state_blob, full_tvb(), rtree, info.cmd)
          if ok then summary = s end
        elseif info.dec_key == "read_input_state" then
          subtree:add(full_tvb(), string.format(
            "Input-state blob (288 bytes) — decoder TBD; see dsp408/protocol.py CAT_INPUT notes"))
        end

        pinfo.cols.info = string.format("%s → REASSEMBLED (%d bytes)%s",
          label, #info.full_payload, summary and (" " .. summary) or "")
      end

      -- Checksum + end marker from last continuation
      if info.checksum ~= nil then
        local chk_off = info.payload_here
        local chk_item = subtree:add(f.checksum, buffer(chk_off, 1))
        chk_item:append_text(" (from last continuation; strict validation skipped — see note)")
        if chk_off + 1 < buffer:len() then
          local em_item = subtree:add(f.end_marker, buffer(chk_off + 1, 1))
          if info.end_marker ~= END_MARKER then
            em_item:append_text(string.format(" (expected 0x%02X)", END_MARKER))
            em_item:add_tvb_expert_info(ef_bad_end, buffer(chk_off + 1, 1))
          else
            em_item:append_text(" (valid)")
          end
        end
      end
    end
  else
    -- We saw a continuation but have no state for it — shouldn't happen under
    -- the heuristic, but guard anyway.
    subtree:add(buffer(), "Orphan continuation (no pending multi-frame on this conversation)")
    pinfo.cols.info = "DSP-408 orphan continuation"
  end

  return buffer:len()
end

-- ── Dissector entry point ─────────────────────────────────────────────

function dsp408.dissector(buffer, pinfo, tree)
  local len = buffer:len()
  if len < 16 then return 0 end

  if len >= 4 and buffer(0, 4):uint() == FRAME_MAGIC then
    return dissect_first_or_single(buffer, pinfo, tree)
  end
  -- Not a magic-bearing frame — must be a continuation
  return dissect_continuation(buffer, pinfo, tree)
end

-- ── Acceptance test for the heuristic ─────────────────────────────────

local function is_dsp408_or_continuation(buffer, pinfo, tree)
  local len = buffer:len()
  if len < 16 then return false end
  -- Magic-bearing frame
  if buffer(0, 4):uint() == FRAME_MAGIC then return true end
  -- 64-byte interrupt URBs matching a pending conversation are continuations
  if len == FRAME_SIZE then
    local key = conversation_key(pinfo)
    local state = pending[key]
    if state then
      -- Sanity-check the lookahead window
      if pinfo.number - state.first_frame <= MAX_CONTINUATION_FRAMES then
        return true
      end
      -- Beyond window — abandon
      per_frame[state.first_frame] = per_frame[state.first_frame] or {}
      per_frame[state.first_frame].abandoned = true
      pending[key] = nil
    end
    -- Also check per_frame memo (re-dissection pass)
    if per_frame[pinfo.number] and per_frame[pinfo.number].role == "continuation" then
      return true
    end
  end
  return false
end

local function dsp408_heuristic(buffer, pinfo, tree)
  if not is_dsp408_or_continuation(buffer, pinfo, tree) then return false end
  dsp408.dissector(buffer, pinfo, tree)
  return true
end

-- ── Registration ─────────────────────────────────────────────────────
local usb_product = DissectorTable.get("usb.product")
if usb_product then
  usb_product:add(0x04835750, dsp408)
end
dsp408:register_heuristic("usb.interrupt", dsp408_heuristic)
dsp408:register_heuristic("usb.bulk",      dsp408_heuristic)

-- ── Init: clear state when a new capture is loaded ───────────────────
function dsp408.init()
  pending   = {}
  per_frame = {}
end
