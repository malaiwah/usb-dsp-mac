# Front-port dongle protocols + simplified ESP32 path

Goal: figure out what protocol(s) live on the DSP-408 front USB-A port without
purchasing a DSP-BT4.0 dongle, then choose the simplest ESP32 implementation strategy.

## What the firmware analysis told us

Strings in `downloads/DSP-408-Firmware-V6.21.bin`:

| Offset | String | Meaning |
|-------:|--------|---------|
| 0x5e38 | `AT+RELD` | Wi-Fi/UART module: reset to defaults |
| 0x5e5c | `AT+ENTM` | Enter transparent transmission mode (USR-IOT / HF-LP family) |
| 0x5e6c | `AT+FASSID=` | First AP SSID config (Wi-Fi) |
| 0x5e80 | `AT+FTCPTO=300` | TCP socket timeout 300 s |
| 0x5e90 | `AT+FMAXSK=1` | Max sockets = 1 (transparent passthrough) |
| 0x111fd | `BLE=SPP=` | BLE-acting-as-SPP marker |
| 0x1120b | `DATA=` | (data marker) |
| 0x11212 | `=GATT` | GATT marker |
| 0x11218 | `Custom` | Custom characteristic / Custom service marker |
| 0x3007 | `@MYDW-AV1.06` | DSP firmware ID with `@` AT-style prefix |

All `AT+...` strings are USR-IOT-style **Wi-Fi-module** AT commands, not Bluetooth. So
the firmware supports a Wi-Fi dongle that exposes a transparent TCP-over-UART bridge.

What's **NOT** in the firmware:
- Standard CDC class functional descriptors (`05 24 00`, `04 24 02`, `05 24 06`)
- Common USB-UART bridge VID/PIDs (CP210x, CH340, FTDI FT232)
- Common BLE module VID/PIDs (CC254x, Realtek BT)

What IS in the firmware:
- **STM32 USB_OTG_FS peripheral** (`0x50000000`) referenced 3 times → host-mode USB
- **STM32 USB device peripheral** (`0x40005C00`) referenced once → device-mode USB (back port HID)
- HID class descriptor + 2× INT endpoints with mps=64 → that's the back USB-B port
- `STM32 CDC PID 0x5750` appears once → our own device descriptor

The combination — USB host hardware + AT commands at UART chunk sizes + no class-descriptor
parsing — strongly indicates the front port runs a **USB CDC-ACM host stack** (STM32 USB
Host Library's CDC class driver), accepting whatever CDC-ACM device the user plugs in.

## What the Android app told us — the real Rosetta Stone

`leon.android.chs_ydw_dcs480_dsp_408/datastruct/Define.java`:

```java
COMMUNICATION_WITH_WIFI             = 0
COMMUNICATION_WITH_BLUETOOTH_SPP     = 1   // default in MacCfg.COMMUNICATION_MODE
COMMUNICATION_WITH_BLUETOOTH_LE      = 2
COMMUNICATION_WITH_USB_HOST          = 3   // ← Android device is USB host to DSP
COMMUNICATION_WITH_UART              = 4
COMMUNICATION_WITH_BLUETOOTH_SPP_TWO = 5

UART_MaxT = 32                  // UART/SPP MTU chunk size
USB_MaxT  = 64                  // USB HID frame size — matches our protocol
USB_DSPHD_VID = 1155 = 0x0483   // STMicroelectronics
USB_DSPHD_PID = 22352 = 0x5750  // DSP-408 back-port HID interface
```

`Define.BT_Paired_Name_DSP_*` lists the dongle product family:
- `"DSP CCS===="`
- `"DSI"`
- `"DSP HDx"`
- `"DSP"` (generic / NAKAMICHI rebrand)
- `"DSP Play====="`

Several BT dongle skus, all from the same OEM, all paired by name prefix.

## So which transports does the front port actually accept?

Based on all evidence:

1. **USB CDC-ACM dongles** (most likely default):
   - BT4.0-SPP dongle: CC2540/HC-05/JDY-style, presents USB-CDC-ACM → UART → BT classic SPP
   - BT4.0-LE dongle: CC2540/HM-10-style, presents USB-CDC-ACM → UART → BLE GATT (the FFE0/FFE1 service leon talks to)
   - Wi-Fi dongle: HF-LPT-style or USR-WIFI232-A2, presents USB-CDC-ACM → UART → TCP socket
2. **Possibly vendor-specific bulk** if the firmware has a custom class driver, but no
   evidence of vendor descriptor matching in the firmware.

Wire framing on the front-port byte stream is the **same `80 80 80 EE | dir | ver | seq |
cat | cmd | len | payload | xor | aa` envelope** as our HID frames, just minus the 1-byte
HID report-ID prefix that the back-port USB device interface tacks on. This is implied by
leon's `ServiceOfCom.java` parser at line 820–874 — the same parser handles BLE, SPP, and
UART byte streams; and the firmware uses the same byte format for both the back-port HID
and the front-port UART (otherwise it'd need two separate state machines).

## The big realisation: ESP32 doesn't need to touch the front port at all

The Android app already supports `COMMUNICATION_WITH_USB_HOST = 3` mode where the **Android
device acts as USB host to the DSP's back USB-B port**, using exactly our HID protocol
(VID/PID 0x0483/0x5750, 64-byte frames). The leon app's USB OTG codepath is the cleanest,
already-shipping example of "control DSP-408 via USB without a dongle."

So the simplest ESP32 path is:

> **ESP32-S3 (or P4) is USB host to the DSP-408's back USB-B port, just like our Pi+Python
> driver, just like the Android-OTG mode.**

Same protocol, smaller form factor, no Pi required, no MQTT broker setup unless the user
wants one (ESPHome's native API is enough for HA integration).

### Hardware

- **ESP32-S3 dev board** (~$10) — has native USB OTG HS host capable, plus Wi-Fi/BLE.
  Recommended chip; well-supported by ESP-IDF + Arduino + ESPHome.
- USB-A male to USB-B male cable (or solder direct to ESP32).
- 5V/500mA power supply (ESP32 + USB host need a stable rail; the DSP's 12V doesn't
  power the ESP, but the ESP can be USB-powered separately or share the DSP's 12V via
  a buck converter).

### Software stack (recommended)

- **ESP-IDF + TinyUSB host** (Espressif's USB Host Library wraps TinyUSB) — supports HID host class
  natively. Enumerate VID/PID 0x0483/0x5750, claim the HID interface, send/receive 64-byte
  reports through INT endpoints.
- Port `dsp408/protocol.py` to C++: ~100 lines for frame builder + parser + xor checksum.
- Port `dsp408/device.py` to a state-tracking class: ~300 lines, mostly the same dict-based
  cache of channel state we already have.
- ESPHome integration: write an `external_components` package that exposes each control
  surface as a stock ESPHome `number`/`switch`/`select`. ESPHome handles HA discovery,
  Wi-Fi config, OTA updates, and MQTT/native-API automatically.

### Effort estimate

- USB host enumeration + HID I/O: ~1 day
- Protocol port (frame + cmds): ~1 day
- State management + cache: ~1 day
- ESPHome wrapping + HA discovery: ~1 day
- Testing on real hardware: ~1 day

**Total: ~1 week of focused work**, with most of the risk in the USB host enumeration
(TinyUSB host on ESP32 is mature but has rough edges). Skip the firmware-update path
(let the Mac/Pi handle that out-of-band).

### Why NOT the front-port-as-CDC approach

It's also possible, but worse:
1. We'd need to figure out the exact CDC class quirks the firmware host accepts (line
   coding format, control transfer sequences, baud rate setting).
2. It only works while no real BT/Wi-Fi dongle is plugged in.
3. We'd be locked out of the back USB-B port for desktop GUI use (the DSP only has one of
   each port; the back port is the standard control surface).
4. No upside: same wire protocol, harder route in.

The only argument for it is "you can keep using the back USB-B for the official Windows
GUI while the ESP runs in the front port." That's a niche case. For a Pi-replacement
project, back-port USB host is cleaner.

## Possible follow-up: support the Wi-Fi dongle protocol in our Python driver

The firmware speaks USR-IOT AT commands to the front port when a Wi-Fi module is detected.
That gives us a tantalizing alternative to the USB control path entirely:

1. Configure a USR-WIFI232 / HF-LPT module to bridge `192.168.x.x:port` ↔ UART.
2. The DSP firmware would talk to the module over USB-UART, but we could also talk to
   the module's TCP socket directly from a remote box via TCP.
3. End result: control the DSP over a TCP socket on Wi-Fi, no Pi needed at all.

This is an interesting parallel path but requires actually buying a Dayton-blessed Wi-Fi
dongle (or building an equivalent from a USR-WIFI232-A2). Out of scope for now.

## Action items

1. **Cross-check this writeup before implementation** — the firmware's actual USB host
   class driver is buried in the binary; if Dayton uses something exotic (e.g. a custom
   class descriptor matching BL_USB or similar), the CDC-ACM hypothesis could be wrong.
   But for the ESP32 path that doesn't matter — we're using the back port.
2. **Sanity test the ESP32 plan** against existing TinyUSB host examples (Espressif has a
   "HID host" reference example using a standard USB keyboard). If that works on the dev
   board, swap target VID/PID to ours and confirm enumeration.
3. **Defer the front-port investigation** to whenever we get a real dongle to dump.
