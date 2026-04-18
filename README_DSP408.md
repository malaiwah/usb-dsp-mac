# DSP-408 Python Interface

## Overview

The DSP-408 can be controlled via two methods:

1. **TCP/IP Network** (Recommended) - Works reliably on all platforms
2. **USB HID** - Requires macOS permissions, may not work reliably

## Connection Methods

### TCP/IP Network (Preferred)

The DSP-408 has a built-in network interface. The official app connects via TCP.

**Default Settings:**
- IP Address: `192.168.3.100`
- Port: `5000` (may vary)

**Setup:**
1. Connect DSP-408 to your network via Ethernet
2. Set your computer's IP to the same subnet (e.g., `192.168.3.x`)
3. Run: `python dsp408_interface.py --tcp`

**Custom IP/Port:**
```bash
python dsp408_interface.py --tcp --ip 192.168.3.100 --port 5000
```

### USB HID

**Note:** On macOS, USB HID requires special permissions and may not work reliably.

**Requirements:**
- macOS: System Settings → Privacy & Security → Input Monitoring (add Terminal/Python)
- Linux: udev rules for USB access
- Windows: No special requirements

**Usage:**
```bash
python dsp408_interface.py --usb
```

## Protocol

The DSP-408 uses DLE/STX framing over either USB HID (64-byte reports) or TCP:

```
Frame Format:
  [STX] [Seq Hi] [Seq Lo] [Cmd] [Data...] [ETX] [Checksum]
  0x10  0x02     0x00     0x01  0x10     0x10 0x03  XOR all

Example Handshake:
  TX: 10 02 00 01 01 10 10 03 11
  RX: 10 02 01 00 02 10 13 10 03 01
```

**Commands:**
| Cmd  | Name         | Description                    |
|------|--------------|--------------------------------|
| 0x10 | HANDSHAKE    | Initial connection handshake   |
| 0x13 | DEVICE_INFO  | Get device name/version        |
| 0x12 | STATUS       | Get current status             |
| 0x40 | KEEPALIVE    | Keep alive + meter levels      |
| 0x2C | PRESET_CNT   | Get preset count               |
| 0x29 | GET_PRESET   | Get preset name                |
| 0x27 | CONFIG_DUMP  | Get config chunk               |
| 0x34 | SET_GAIN     | Set channel gain               |
| 0x35 | SET_MUTE     | Set channel mute               |
| 0x48 | SET_GEQ      | Set GEQ band                   |
| 0x33 | SET_PEQ      | Set PEQ band                   |
| 0x32 | SET_HPF      | Set high-pass filter           |
| 0x31 | SET_LPF      | Set low-pass filter            |
| 0x3A | SET_MATRIX   | Set matrix routing             |

## Usage Examples

### Basic Connection
```python
from dsp408_interface import DSP408

dsp = DSP408()  # Auto-detect
if dsp.connect():
    print("Connected!")
    
    # Get device info
    info = dsp.get_device_info()
    print(f"Device: {info.device_name}")
    print(f"Firmware: {info.firmware_version}")
    
    # Get presets
    count = dsp.get_preset_count()
    for i in range(count):
        name = dsp.get_preset(i)
        print(f"Preset {i+1}: {name}")
    
    dsp.disconnect()
```

### Force TCP Connection
```python
from dsp408_interface import DSP408

dsp = DSP408(ip="192.168.3.100", port=5000, force_tcp=True)
if dsp.connect():
    # Send keepalive to get meter levels
    meters = dsp.keepalive()
    print(f"Meter levels: {meters}")
    dsp.disconnect()
```

### USB Connection (macOS)
```python
from dsp408_interface import DSP408

# May require sudo and Input Monitoring permission
dsp = DSP408(force_usb=True)
if dsp.connect():
    dsp.handshake()
    # ... use device
    dsp.disconnect()
```

## Files

- `dsp408_interface.py` - Main Python interface (USB + TCP)
- `dsp408_tcp.py` - TCP-only interface
- `dsp408.py` - USB HID interface (legacy)
- `talk.py` - Original USB test script
- `talk_iokit.py` - macOS IOKit USB test (experimental)

## Troubleshooting

### USB Not Working on macOS

1. Go to System Settings → Privacy & Security → Input Monitoring
2. Add Terminal (or your IDE/Python)
3. Restart Terminal
4. Try running with `sudo`

### TCP Not Connecting

1. Verify DSP-408 is connected to network (Ethernet cable)
2. Check device IP in DSP-408 menu settings
3. Ensure your computer is on the same subnet
4. Try scanning ports: `nmap 192.168.3.100`

### No Response to Commands

The device may require a specific initialization sequence. See the Flutter app code in `dsp-408-ui/` for the complete initialization flow.

## Protocol Analysis

The protocol was reverse-engineered from:
- Firmware binary (`downloads/DSP-408-Firmware.bin`)
- USB traffic capture (`dsp-408-ui/pcap_error.txt`)
- Flutter app source (`dsp-408-ui/lib/devices/t_racks408/`)

Key findings:
- Device uses standard USB HID class (VID=0x0483, PID=0x5750)
- Primary control is via TCP/IP, not USB
- Protocol uses DLE/STX framing with XOR checksum
- Sequence bytes: `00 01` for host→device, `01 00` for device→host

## References

- Official Windows software: `downloads/DSP-408-Windows-V1.24.zip`
- Flutter UI source: `dsp-408-ui/`
- Protocol capture: `dsp-408-ui/pcap_error.txt`
