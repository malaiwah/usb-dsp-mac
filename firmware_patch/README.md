# DSP-408 firmware patching experiment

This directory holds two minimally-patched copies of the publicly-distributed
**DSP-408 firmware V6.21** plus the script that generated them. The goal is
to answer one question with a single Windows upload session:

> **Does the device's bootloader actually validate the firmware bytes?**

If it doesn't (or only does a weak/unverified check), the macOS HID-input
problem is solved permanently by flashing `PATCHED-hidpage.bin`.

---

## Files

| File | Size | SHA-256 | What changed |
|---|---|---|---|
| `../downloads/DSP-408-Firmware-V6.21.bin` | 70,296 | `97a4e23c…29268` | original (recovery copy) |
| `DSP-408-Firmware-V6.21-PATCHED-hidpage.bin` | 70,296 | `b5085a09…27d7` | 1 byte: `[0xB8C0] 0x8C → 0x0C` |
| `DSP-408-Firmware-V6.21-PATCHED-noop.bin` | 70,296 | `e2f544a5…0c534` | 1 byte: `[0xB94B] 0x00 → 0x01` |

Re-verify any time:

```bash
shasum -a 256 \
  ../downloads/DSP-408-Firmware-V6.21.bin \
  DSP-408-Firmware-V6.21-PATCHED-hidpage.bin \
  DSP-408-Firmware-V6.21-PATCHED-noop.bin
```

Re-derive from the script (idempotent):

```bash
python3 patch_firmware.py
```

---

## What each patch does

### `PATCHED-hidpage.bin` — the actually-useful patch

Single byte at file offset **`0xB8C0`**, inside the embedded HID Report
Descriptor:

| | bytes 0xB8BF..0xB8DF (the descriptor) |
|---|---|
| original | `05 8c 09 01 a1 01 09 03 15 00 26 00 ff 75 08 95 40 81 02 09 04 ... c0` |
| patched  | `05 **0c** 09 01 a1 01 09 03 15 00 26 00 ff 75 08 95 40 81 02 09 04 ... c0` |

The `05 XX` HID short-form item sets *Usage Page*. We change it from:

- **0x8C — Bar Code Scanner** (recognized by macOS but with no event driver,
  so userspace input reports are dropped by `HIDDefaultBehavior=""`)

to:

- **0x0C — Consumer Control** (heavily handled by macOS `hidd`, gets full
  user-space input report delivery).

Everything else is unchanged — same vendor protocol on the wire, same
Output report semantics, same VID/PID.

### `PATCHED-noop.bin` — control / bootloader probe

Single byte at file offset **`0xB94B`**, in a long zero-padding run between
USB string descriptors. The byte itself is not read by the firmware; the
firmware behaves identically. The point is purely to ask the bootloader:
*"do you care if a byte changes anywhere in this image?"*

---

## Test plan (Windows side)

> ⚠ Keep `DSP-408-Firmware-V6.21.bin` accessible. It's the recovery image.
> Keep the device plugged in for the full procedure.

1. **Baseline** — open the V1.24 GUI, confirm the device connects, note
   firmware version reported in the GUI's About/info area (this is the only
   way we'll learn the device's actual firmware version).

2. **Upload `PATCHED-noop.bin` first** (lower-risk experiment):
   * If accepted with **"Update successfully"** → bootloader does **not**
     enforce whole-image integrity. Proceed to step 3.
   * If accepted but device misbehaves on next boot → flash the original
     `DSP-408-Firmware-V6.21.bin` to recover.
   * If rejected with **"Update file is error!"** or **"Update version
     error!"** → bootloader (or .exe) is doing integrity validation.
     **Stop. Do not proceed to step 3.** Note the exact error message —
     it tells us which layer rejected.

3. **Upload `PATCHED-hidpage.bin`**:
   * If accepted → 🎉 **plug into Mac**, run
     `macos_hid_diag/dump_hid_descriptor` and confirm Usage Page now reads
     `0x0C` instead of `0x8C`. Then run `macos_hid_diag/try_alt_report_types`
     and look for `*** RX #1 …` callbacks. If they appear, the macOS bug is
     gone permanently for this unit.
   * If rejected → flip back: integrity check exists but is selective,
     possibly per-region. Note the error and flash the original to recover.

4. **Always finish by flashing the original** `DSP-408-Firmware-V6.21.bin`
   if you don't intend to keep the patched version, just to remove ambiguity
   for any future debugging session.

---

## Expected outcomes — truth table

| `noop` upload | `hidpage` upload | Conclusion |
|---|---|---|
| ✅ accepted | ✅ accepted | **No integrity check.** Patched firmware is keeper. |
| ✅ accepted | ❌ rejected | Selective per-region check (hashes only "important" sections). The HID descriptor IS in a checked region. Need bootloader RE. |
| ❌ rejected | (don't try)  | **Whole-image integrity check.** Almost certainly a CRC, hash, or signature over the full payload. Need bootloader RE — single-byte patches can't pass. |
| ❌ rejected | ✅ accepted  | (Inconsistent — would suggest the .exe rejects only on the noop offset specifically. Very unlikely.) |

The .exe error wording tells us *who* rejected:

* `"Update file is error!"` / `"Update version error!"` → the .exe rejected
  before sending (parses the .bin header/version itself).
* `"The firmware update failed!"` after upload progress → the device
  rejected during/after upload.

---

## Why this is a worthwhile experiment

Static analysis of `DSP-408.exe` (V1.24) found:

- Zero crypto API imports (no `BCryptHashData`, no OpenSSL, no mbedTLS).
- The 8-byte trailer following `/ZZZ` (`ee a4 1a 05 a5 11 02 16` in V6.21)
  doesn't match any standard CRC / sum / hash across 7 payload windows.
- The .exe's only HID functions are `HidD_GetHidGuid` + `HidD_GetAttributes`;
  it talks raw via `CreateFileA` + `ReadFile`/`WriteFile`.
- The firmware payload itself is **not** embedded in the .exe (so the .exe
  literally just streams whichever .bin the user picks via a file dialog).

That's all consistent with **either** interpretation:

1. The trailer is a real cryptographic signature validated only on-device
   by a bootloader we don't have a copy of, OR
2. The trailer is a build-time stamp / weak checksum / unverified padding
   the bootloader doesn't actually check.

The only cheap way to disambiguate is to flash a known-modified image and
see what happens.

---

## Recovery if anything goes wrong

* If the device still enumerates as USB VID 0x0483 / PID 0x5750 with the
  V1.24 GUI recognizing it → flash `DSP-408-Firmware-V6.21.bin` from this
  repo (`downloads/`).
* If the device disappears entirely from USB → it likely entered the STM32
  ROM bootloader (DFU). On Windows, install the **STMicroelectronics DfuSe**
  utility; the device will appear as `STM32 BOOTLOADER` (VID 0x0483 /
  PID 0xDF11). On macOS, `dfu-util -l` (already installed via brew). Either
  can re-flash `DSP-408-Firmware-V6.21.bin` directly to flash address
  `0x08005000` (skip the 8-byte WMCU header — start the upload at file
  offset 8).
* If neither path works, the device is hard-bricked and would need physical
  SWD pin access for recovery.
