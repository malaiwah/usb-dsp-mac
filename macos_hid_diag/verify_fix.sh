#!/bin/bash
# Run after: sudo /tmp/force_hid_prop && sudo launchctl kickstart -k system/com.apple.hidd
echo "=== Verifying HID fix ==="
sleep 2

echo -n "IOHIDInterface.HIDDefaultBehavior: "
ioreg -r -c "AppleUserUSBHostHIDDevice" -d 6 2>/dev/null | grep '"HIDDefaultBehavior"' | tail -1

echo -n "IOHIDInterface.DeviceOpenedByEventSystem: "
ioreg -r -c "AppleUserUSBHostHIDDevice" -d 6 2>/dev/null | grep "DeviceOpenedByEventSystem" | head -1

echo -n "IOHIDInterface.DebugState: "
ioreg -r -c "AppleUserUSBHostHIDDevice" -d 8 2>/dev/null | grep '"DebugState".*CreatedBuffers' | head -1

echo ""
echo "hidutil list (DSP-408 entry):"
hidutil list 2>&1 | grep -E "0x483|5750|Audio_Equip" || echo "  (not found in hidutil list)"

echo ""
echo "Running CoreHID test for 10s..."
/tmp/corehid_fix2 2>&1 | head -20
