#!/bin/bash
# Patch hidapi to open devices in non-exclusive mode on macOS
# Based on: https://github.com/libusb/hidapi/issues/769
#           https://github.com/signal11/hidapi/issues/400

set -e

echo "=== Patching hidapi for macOS non-exclusive access ==="

# Check if we're on macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "Error: This patch is for macOS only"
    exit 1
fi

# Find hidapi installation
HIDAPI_PATH=$(python3 -c "import hid; import os; print(os.path.dirname(hid.__file__))" 2>/dev/null)

if [ -z "$HIDAPI_PATH" ]; then
    echo "hidapi not found. Installing..."
    pip3 install hidapi
    HIDAPI_PATH=$(python3 -c "import hid; import os; print(os.path.dirname(hid.__file__))")
fi

echo "Found hidapi at: $HIDAPI_PATH"

# Check if it's a source installation or wheel
if [[ "$HIDAPI_PATH" == *".dist-packages"* ]] || [[ "$HIDAPI_PATH" == *".venv"* ]] || [[ "$HIDAPI_PATH" == *"site-packages"* ]]; then
    # It's installed, we need to patch the compiled library or source
    
    # Look for the mac hid.c or hid.o file
    MAC_HID_PATH=$(find "$HIDAPI_PATH" -name "hid.c" -o -name "hidapi_mac*" 2>/dev/null | head -1)
    
    if [ -z "$MAC_HID_PATH" ]; then
        echo ""
        echo "Note: hidapi is installed as a wheel (pre-compiled)"
        echo "To patch, you need to reinstall from source:"
        echo ""
        echo "  pip3 uninstall hidapi -y"
        echo "  pip3 install hidapi --no-binary :all:"
        echo ""
        echo "Or clone and patch the source:"
        echo "  git clone https://github.com/libusb/hidapi.git"
        echo "  cd hidapi/mac"
        echo "  # Edit hid.c, remove kIOHIDOptionsTypeSeizeDevice"
        echo "  python3 setup.py install"
        exit 1
    fi
    
    echo "Found hidapi source at: $MAC_HID_PATH"
    
    # Check if already patched
    if grep -q "kIOHIDOptionsTypeNone" "$MAC_HID_PATH" 2>/dev/null; then
        echo "✓ hidapi already patched for non-exclusive mode"
        exit 0
    fi
    
    # Create backup
    cp "$MAC_HID_PATH" "$MAC_HID_PATH.backup"
    echo "Created backup: $MAC_HID_PATH.backup"
    
    # Patch the file - replace kIOHIDOptionsTypeSeizeDevice with kIOHIDOptionsTypeNone
    # This is typically in the IOHIDDeviceOpen call
    
    if grep -q "kIOHIDOptionsTypeSeizeDevice" "$MAC_HID_PATH"; then
        sed -i '' 's/kIOHIDOptionsTypeSeizeDevice/kIOHIDOptionsTypeNone/g' "$MAC_HID_PATH"
        echo "✓ Replaced kIOHIDOptionsTypeSeizeDevice with kIOHIDOptionsTypeNone"
    else
        echo "Note: kIOHIDOptionsTypeSeizeDevice not found"
        echo "The file may use a different pattern or already be non-exclusive"
    fi
    
    # Reinstall from patched source
    echo ""
    echo "Rebuilding hidapi from patched source..."
    
    # Find setup.py
    SETUP_PATH=$(find "$HIDAPI_PATH" -name "setup.py" 2>/dev/null | head -1)
    
    if [ -n "$SETUP_PATH" ]; then
        SETUP_DIR=$(dirname "$SETUP_PATH")
        cd "$SETUP_DIR"
        python3 setup.py install
        echo "✓ hidapi rebuilt with non-exclusive mode"
    else
        echo "Could not find setup.py for rebuild"
        echo "You may need to manually rebuild hidapi"
    fi
    
else
    echo "Unknown hidapi installation type"
    exit 1
fi

echo ""
echo "=== Patch complete ==="
echo "Restart your Python application to use the patched hidapi"
