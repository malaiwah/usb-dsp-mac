# DSP-408 macOS USB HID Fix

## Problem

The DSP-408 USB HID interface works on Windows but not on macOS. The device enumerates correctly but never responds to commands.

**Root Cause:** hidapi on macOS opens HID devices in **exclusive mode** (`kIOHIDOptionsTypeSeizeDevice`), which prevents the device from responding. Windows opens devices in **non-exclusive mode** by default.

## Solutions

### Solution 1: Patch hidapi (Recommended)

Run the patch script to rebuild hidapi with non-exclusive mode:

```bash
chmod +x patch_hidapi_macos.sh
./patch_hidapi_macos.sh
```

This will:
1. Find your hidapi installation
2. Replace `kIOHIDOptionsTypeSeizeDevice` with `kIOHIDOptionsTypeNone`
3. Rebuild hidapi from source

**Manual patch (if script fails):**

```bash
# Uninstall wheel version
pip3 uninstall hidapi -y

# Install from source (will be patched during install)
pip3 install hidapi --no-binary :all:

# Or clone and patch manually
git clone https://github.com/libusb/hidapi.git
cd hidapi/mac
# Edit hid.c, line ~XXX: change kIOHIDOptionsTypeSeizeDevice to kIOHIDOptionsTypeNone
cd ..
python3 setup.py install
```

### Solution 2: Use IOKit Directly

Use the provided `dsp408_iokit.py` module which bypasses hidapi entirely:

```python
from dsp408_iokit import DSP408IOKit

dev = DSP408IOKit()
if dev.connect():
    dev.write(command_bytes)
    response = dev.read(64)
    dev.disconnect()
```

**Advantages:**
- No hidapi dependency
- Guaranteed non-exclusive access
- Full control over IOKit parameters

**Disadvantages:**
- macOS only
- More complex code
- May need run loop integration for async reads

### Solution 3: Use TCP/IP (Works Now)

The TCP/IP interface works on all platforms without modification:

```python
from dsp408_tcp import DSP408TCP

dev = DSP408TCP("192.168.3.100", 5000)
dev.connect()
response = dev.send_command(0x40)  # Keepalive
dev.disconnect()
```

## Testing

After applying the patch:

```bash
# Test patched USB interface
python3 dsp408_usb_patched.py

# Or test IOKit interface
python3 dsp408_iokit.py

# Test TCP/IP (should always work)
python3 dsp408_tcp.py
```

## Expected Results

**Before patch:**
```
✓ Device opens successfully
✓ HID writes succeed (no error)
✗ Device NEVER responds to any command
```

**After patch:**
```
✓ Device opens (non-exclusive)
✓ HID writes succeed
✓ Device responds to commands
```

## Technical Details

### hidapi Exclusive Mode

In `hidapi/mac/hid.c`:
```c
// BEFORE (exclusive - blocks device):
res = IOHIDDeviceOpen(device, kIOHIDOptionsTypeSeizeDevice);

// AFTER (non-exclusive - shares device):
res = IOHIDDeviceOpen(device, kIOHIDOptionsTypeNone);
```

### Why Windows Works

Windows HID API (`HidD_OpenDevice`) opens devices non-exclusively by default. No special flags needed.

### Why macOS Fails

macOS IOKit has two open modes:
- `kIOHIDOptionsTypeSeizeDevice` (0x00000001) - Exclusive, grabs device from all other processes
- `kIOHIDOptionsTypeNone` (0x00000000) - Non-exclusive, shares device

hidapi chose exclusive mode, which breaks devices that expect to communicate bidirectionally while being monitored.

## Related Issues

- [hidapi #769](https://github.com/libusb/hidapi/issues/769) - hid.write() on macOS 15.5 stops hardware
- [hidapi #749](https://github.com/libusb/hidapi/issues/749) - IOHIDDeviceSetReport failed (0xE0005000)
- [hidapi #400](https://github.com/signal11/hidapi/issues/400) - Non-exclusive open request (solved)

## Files

- `patch_hidapi_macos.sh` - Automated patch script
- `dsp408_iokit.py` - Direct IOKit implementation
- `dsp408_usb_patched.py` - USB interface with auto-detection
- `dsp408_tcp.py` - TCP/IP interface (works without patch)
- `dsp408_interface.py` - Unified interface (USB + TCP)

## Quick Start

```bash
# Option 1: Patch hidapi
./patch_hidapi_macos.sh
python3 dsp408_interface.py --usb

# Option 2: Use TCP (no patch needed)
python3 dsp408_interface.py --tcp --ip 192.168.3.100

# Option 3: Use IOKit directly
python3 dsp408_iokit.py
```
