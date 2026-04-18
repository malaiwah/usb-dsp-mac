# USB packet captures for DSP-408 reverse engineering

> **If you are a Claude session running on the user's Windows desktop**, this
> file is your brief. Your job is to **produce the `windows-*.pcapng`
> captures listed under "What we need from Windows" below**. Do not modify
> the Linux captures or analyzer; treat them as reference data. When done,
> commit the new files and push — the repo remote is already configured.

---

## Mission, in one sentence

Control a Dayton Audio DSP-408 (VID 0x0483 / PID 0x5750) from macOS over
USB. The blocker is that the vendor command protocol over USB is unknown —
we need Windows captures of the official DSP-408.exe V1.24 to learn it.

## What we already know (from Linux captures — DO NOT redo these)

The Linux captures in this directory already answered several questions:

| Question | Answer | Source |
|---|---|---|
| Does the device emit input reports at idle? | **No** (0 bytes in 30 s) | `linux-01-enum-idle.pcapng` |
| Does the HID Report Descriptor on the wire match the V6.21 firmware .bin? | **Yes, byte-for-byte** | `linux-01-enum-idle.txt` |
| Does the device respond to WMCU-framed cmds 0x01..0xA0 via interrupt OUT? | **No** (0 replies to 20 commands) | `linux-02-vendor-probe.pcapng` |
| Does HID SET_REPORT / GET_REPORT work as a transport? | **No** (no feature reports declared) | `linux-02b-setreport-probe.pcapng` |
| Is the macOS "no input reports" behaviour caused by macOS? | **No — the device genuinely isn't replying.** | combined |

Takeaway: either the command framing is wrong, or the command bytes we
tried aren't valid handlers, or there's a handshake we haven't seen. A
Windows capture of the official GUI is the only way to disambiguate.

**Prior-art caveat**: `../README_DSP408.md` documents a DLE/STX protocol
with a HANDSHAKE cmd 0x10 / KEEPALIVE cmd 0x40 / etc. That protocol was
reverse-engineered from a **TCP capture** of the device's network
interface — not USB. Our Linux probe sent exactly that framing (byte-for-
byte identical to the HANDSHAKE example in `README_DSP408.md`) on USB and
got no reply, so either USB uses a different envelope entirely or it
requires a step TCP doesn't. Windows captures resolve this.

---

## What we need from Windows

Four captures, in priority order. Each gets named as shown, dropped in
`captures/`, accompanied by a sibling `.txt` note.

### Priority 1 — firmware update of ORIGINAL V6.21 (single highest-value file)

`windows-01-fw-update-original-V6.21.pcapng`

Why this one: the .exe is just a thin HID wrapper that streams whichever
`.bin` file you pick via `CreateFileA`+`WriteFile` (confirmed by static
analysis in `../macos_hid_diag/exe_analysis/`). It has **zero crypto API
imports** and the firmware is **not embedded** in the .exe. So the bytes
on the wire during firmware update = the handshake/framing bytes ⊕ the
`.bin` contents. We have the `.bin` locally
(`../downloads/DSP-408-Firmware-V6.21.bin`, SHA-256
`97a4e23c315fbccdb5aff4cd5d6673643bb902b9138493b362df664581729268`), so
**diffing captured payloads against the file mechanically recovers the
upload protocol** — block size, framing wrapper, handshake, ack format.

Do this:

1. Install Wireshark-for-Windows (https://www.wireshark.org/) — during
   setup, tick the box for **USBPcap**. Reboot once (the driver needs to
   register).
2. Plug the DSP-408 into the Windows box. Don't open the .exe yet.
3. Open Wireshark. You'll see `USBPcap1`, `USBPcap2`, … one per root hub.
   Click each; the right pane shows the attached device tree. Pick the
   one listing VID `0483` / PID `5750` (it may display as
   "Audio_Equipment" or "STMicroelectronics"). Remember the interface
   number.
4. Run `downloads/DSP-408-Windows/DSP-408.exe` (V1.24). Confirm the GUI
   shows the device connected.
5. **Start the Wireshark capture** on the USBPcap interface from step 3.
   Note the wall-clock time in the `.txt` sidecar.
6. In the GUI: trigger Firmware Update and pick
   `downloads/DSP-408-Firmware-V6.21.bin` (**the original, not a
   patched copy**). Wait for the "Update successfully" message (or an
   error — capture that too).
7. Stop the Wireshark capture. File → Save As, pcapng, named
   `captures/windows-01-fw-update-original-V6.21.pcapng`.
8. Write `captures/windows-01-fw-update-original-V6.21.txt` with: time
   started, time finished, any GUI messages, any errors, the USBPcap
   interface name/number.

### Priority 2 — app connect + a few settings changes

`windows-04-app-connect-and-settings.pcapng`

Goal: see the framing for regular (non-firmware) commands. This reveals
the day-to-day command set we need to reproduce from macOS.

1. Unplug + replug the DSP-408 (so Windows re-enumerates it) — makes the
   start of the capture easier to read.
2. Start capture on the right USBPcap interface. `t=0` should be **no
   .exe running**.
3. Open DSP-408.exe; let the GUI discover the device and fully load
   whatever state it pulls.
4. Perform, with ~3 s between each (so packets are visually delimited):
   * change **one** EQ band's gain
   * change **one** crossover frequency
   * change a **channel routing** matrix cell
   * change a **volume**
   * save a preset, then load a different preset
5. Stop, save as `windows-04-app-connect-and-settings.pcapng` + `.txt`.

### Priority 3 — (optional) patched-firmware experiment

See `../firmware_patch/README.md` for the full test plan, truth table, and
recovery steps. Two minimally-patched firmware images are already built:

* `firmware_patch/DSP-408-Firmware-V6.21-PATCHED-noop.bin` — 1-byte flip
  in zero-padding. Tests *"does the bootloader verify whole-image
  integrity?"*
* `firmware_patch/DSP-408-Firmware-V6.21-PATCHED-hidpage.bin` — 1-byte
  change to the HID Report Descriptor Usage Page (`0x8C` Bar Code Scanner
  → `0x0C` Consumer Control). Tests whether a tiny descriptor change
  makes macOS HID happy.

Capture each upload attempt — `windows-02-fw-update-patched-noop.pcapng`
then (only if noop succeeds) `windows-03-fw-update-patched-hidpage.pcapng`
— so we know which layer accepted/rejected. **Always finish by reflashing
the original `DSP-408-Firmware-V6.21.bin`** to avoid ambiguity later.

If you brick the device, recovery is via STM32 DFU: device appears as VID
`0483` / PID `DF11`, use DfuSe on Windows or `dfu-util -l` / `dfu-util -a
0 -s 0x08005000 -D DSP-408-Firmware-V6.21.bin` on macOS (the first 8
bytes of the .bin are the WMCU container header; start the DFU upload at
file offset 8 when using dfu-util). Full recovery procedure in
`../firmware_patch/README.md`.

### (Optional) Priority 4 — baseline dump of any device info

Nothing critical, just nice to have: run any "export settings" / "save
config" feature the GUI exposes, while capturing. Named
`windows-05-config-export.pcapng`.

---

## File-naming convention

Preserve these names exactly so the analyzer finds them:

```
captures/
  linux-01-enum-idle.pcapng                 ✓ already captured
  linux-02-vendor-probe.pcapng              ✓ already captured
  linux-02b-setreport-probe.pcapng          ✓ already captured
  windows-01-fw-update-original-V6.21.pcapng   ← priority 1
  windows-02-fw-update-patched-noop.pcapng     ← priority 3a (optional)
  windows-03-fw-update-patched-hidpage.pcapng  ← priority 3b (optional)
  windows-04-app-connect-and-settings.pcapng   ← priority 2
  windows-05-config-export.pcapng              ← optional
```

Each capture gets a sibling `.txt` file with:
* Wireshark interface used (`USBPcapN`)
* The exact time each step happened (rough, but noted)
* What you did during the capture
* Any error messages from the GUI, transcribed verbatim

---

## For the Windows Claude: how to deliver

1. Confirm Wireshark + USBPcap are installed (ask the user to install if
   not — rebooting may be required after install).
2. Find the USBPcap interface that sees the DSP-408. Tell the user
   which interface and verify with them.
3. Walk the user through the priority-1 capture first. You can open pcaps
   locally with `tshark -r file.pcapng -q -z io,phs` to sanity-check
   they're non-empty and contain the right device before declaring done.
4. Drop pcapng + txt files in `captures/`, run
   `python3 captures/analyze_capture.py captures/windows-01-*.pcapng`
   to print a summary. Include the summary in your commit message.
5. `git add captures/`, commit with a clear message, push to the
   already-configured `origin`. (Do **not** force-push, do **not** amend
   existing commits.)

---

## Running the analyzer locally

```bash
# Needs tshark (brew install wireshark on macOS, comes with Wireshark on Win)
python3 captures/analyze_capture.py captures/linux-02-vendor-probe.pcapng
python3 captures/analyze_capture.py captures/windows-01-*.pcapng
```

It prints enumeration + every bulk/interrupt transfer with WMCU frame
decoding for 64-byte OUTs. For captures where the device actually replies,
the "Interrupt-IN completions with data" list at the bottom is the thing
to look at — those are the bytes the device sent back.

---

## What the macOS side will do once Windows captures arrive

Not your job, just for context — so you can leave hints in commit
messages if something looked unusual during capture:

1. Extract OUT payloads in time order and align against
   `DSP-408-Firmware-V6.21.bin` byte-for-byte. Any OUT bytes not in the
   file are framing/handshake.
2. Extract IN payloads — those are the ack/handshake bytes.
3. Use the per-bit alignment to reverse the block size, header format,
   and checksum/CRC (if any) used for firmware transport.
4. Apply the same technique to the settings capture to recover the
   regular command set.
5. Generate `dsp408.py` as a protocol library and a macOS flasher on
   top of it.

---

## Linux capture reference (already done — documentation only)

### One-time setup (on the Pi / any Debian box)

```bash
sudo apt install tshark libhidapi-libusb0 python3-hid usbutils
sudo modprobe usbmon
ls /sys/kernel/debug/usb/usbmon/     # confirm 0s 0u 1s 1t 1u appear
```

### Which `usbmon` interface?

```bash
lsusb | grep 0483:5750
#  Bus 001 Device 004: ID 0483:5750 STMicroelectronics ...
# → bus 1 → capture on usbmon1
```

(*lsusb mis-labels this VID:PID as "STMicroelectronics LED badge" due to a
stale entry in the shared `usb.ids` database. It is not an LED badge.*)

### Reproducing the Linux captures

The two helper scripts live next to this README:

* `_pi_run_captures.sh` — does capture-1 (enum+idle, via
  `/sys/bus/usb/devices/1-1.5/authorized=0/1` to simulate unplug) and
  capture-2 (the probe sequence). Runs probe.py under a live capture.
* `_pi_probe_setreport.sh` — the follow-up capture that tried
  SET_REPORT / GET_REPORT paths (all dead ends).

The probe frame layout is:
```
0x10 0x02 0x00 0x01  N  CMD  0x10 0x03  CHK   + zero-pad to 64
 DLE  STX   seq(H,L)             DLE  ETX   CHK = N XOR CMD XOR payload
```
Output reports go on EP 0x01 (interrupt OUT). The declared interrupt IN
is EP 0x82, and the HID Report Descriptor at firmware offset 0xB8BF
declares matching 64-byte INPUT and OUTPUT reports.

### Serial number

The device's USB serial string is `4EAA4B964C00` — 48 bits, very likely
half of the STM32 96-bit unique ID. Useful if we ever want per-unit
fingerprinting; not used in the protocol.
