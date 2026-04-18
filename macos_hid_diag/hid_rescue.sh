#!/bin/bash
# hid_rescue.sh — Run as root: set HIDDefaultBehavior=Yes on IOHIDInterface, restart hidd
# Usage: sudo /tmp/hid_rescue.sh

set -e

echo "=== HID Rescue Script ==="
echo "Running as: $(whoami)"

/tmp/force_hid_prop
echo ""
echo "Property set. Checking ioreg..."
ioreg -r -c "IOHIDInterface" -d 4 2>/dev/null | grep "HIDDefault\|DebugState\|CreatedBuffers" | head -20

echo ""
echo "Restarting hidd via launchctl..."
launchctl kickstart -k system/com.apple.hidd
sleep 2

echo ""
echo "Waiting 3s for hidd to initialize..."
sleep 3

echo ""
echo "=== hidutil list (after restart) ==="
hidutil list 2>&1 | grep -v "SMC\|PMU\|ANS\|Bluetooth" | head -30

echo ""
echo "=== ioreg IOHIDInterface state ==="
ioreg -r -c "IOHIDInterface" -d 4 2>/dev/null | grep "HIDDefault\|DebugState\|CreatedBuffers\|DeviceOpened" | head -20
