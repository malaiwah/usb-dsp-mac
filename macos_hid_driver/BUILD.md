# DSP408HIDDriver — Build & Install Guide

## Why this driver exists

`IOHIDInterface.HIDDefaultBehavior` is set to `""` (empty) by Apple's generic
`AppleUserHIDDrivers.dext` for every USB HID device with usage page 0 / usage 0
(including the DSP-408).  With an empty string `hidd` never calls
`IOHIDInterface::open()`, so `CreatedBuffers` stays 0 and **all** input-report
delivery paths fail — `IOHIDDevice` callbacks, CoreHID, everything.

`IORegistryEntrySetCFProperties` returns `kIOReturnNotPermitted (0xe00002c7)` for
ALL `IOHIDInterface` services from any user-space caller regardless of privilege.
The only supported way to set the property is via an IOKitPersonalities entry in a
matched driver.  This DEXT is that driver.

---

## Files

| File | Purpose |
|------|---------|
| `DSP408HIDDriver.iig` | DriverKit interface (processed by Xcode's `iig` tool) |
| `DSP408HIDDriver.cpp` | Implementation; calls `super::Start()` which opens `IOHIDInterface` |
| `Info.plist` | Bundle + IOKitPersonalities with `HIDDefaultBehavior=Yes`, VID/PID match |
| `DSP408Driver.entitlements` | Required DriverKit entitlements |
| `test_after_driver.swift` | User-space verification tool |
| `dext_activate_host.swift` | Optional: programmatic activation via `OSSystemExtensionManager` |

All paths in this guide use `~/Code/usb_dsp_mac/macos_hid_driver/` as the project
root (where you're reading this file).

---

## Prerequisites

* Xcode 14+ (macOS 13+ SDK)
* Apple Developer account
* **For local/dev testing**: SIP disabled (see below) — no entitlement approval needed
* **For distribution**: Request entitlements from Apple (developer.apple.com → DriverKit)

---

## Option A — Development testing (SIP disabled, no Apple approval needed)

### 1. Disable SIP

Boot into Recovery Mode (`⌘+R` on Intel, hold power on Apple Silicon), open
Terminal, then:

```bash
csrutil disable
# (Apple Silicon also requires:)
csrutil enable --without kext
```

Reboot normally.

### 2. Enable DriverKit developer mode

```bash
systemextensionsctl developer on
```

### 3. Create the Xcode project

1. **File → New → Project → macOS → Driver Extension** (or System Extension)
2. Product name: `DSP408HIDDriver`
3. Bundle identifier: `com.example.DSP408HIDDriver` (must match Info.plist)
4. Replace generated source files with the files in this directory
5. In the target's **Build Phases → Compile Sources**, make sure
   `DSP408HIDDriver.iig` and `DSP408HIDDriver.cpp` are listed
6. Set **Build Settings → Framework Search Paths** to include the HIDDriverKit
   framework path (usually automatic with the macOS SDK)
7. Replace the target's `Info.plist` with this directory's `Info.plist`
8. Replace the target's entitlements file with `DSP408Driver.entitlements`

### 4. Build and install

```bash
# Build from command line (or use Xcode's Run button):
xcodebuild -scheme DSP408HIDDriver -configuration Debug \
    DRIVERKIT_DEPLOYMENT_TARGET=20.0 \
    CODE_SIGN_IDENTITY="-" \      # ad-hoc signing for SIP-disabled testing
    build

# Install to DriverExtensions folder (requires SIP disabled):
sudo cp -R build/Debug/DSP408HIDDriver.dext /Library/DriverExtensions/

# Activate the extension:
sudo systemextensionsctl install /Library/DriverExtensions/DSP408HIDDriver.dext
```

Or, embed the DEXT in a host app and use `OSSystemExtensionManager` to install it
at runtime (the standard distribution pattern).

### 5. Verify the driver loaded

```bash
# Should show "activated and running":
systemextensionsctl list | grep DSP408

# IORegistry should now show IOHIDInterface matched by your driver:
ioreg -r -c IOHIDInterface -d 4 | grep -A 10 "22352"

# CreatedBuffers should be > 0:
ioreg -r -c AppleUserHIDDevice -d 8 | grep CreatedBuffers

# DEXT log messages:
log stream --predicate 'process == "DSP408HIDDriver"' --level debug
```

### 6. Run the user-space test

```bash
cd ~/Code/usb_dsp_mac/macos_hid_driver
swiftc -o test_after_driver test_after_driver.swift \
    -framework IOKit -framework CoreFoundation

./test_after_driver
```

Expected output:
```
IOHIDInterface.CreatedBuffers        = 4    ← non-zero = driver opened the interface
IOHIDInterface.DeviceOpenedByEventSystem = true
✅  CreatedBuffers > 0 — driver is active. Testing HID callbacks...
Found IOHIDDevice: <IOHIDDevice ...>
IOHIDDeviceOpen(seize): 0x0
Sending GET_VERSION command (0x05) via IOHIDDeviceSetReport …
IOHIDDeviceSetReport: 0x0
Waiting 3s for input report callbacks …
*** RX #1  type=1 id=0 len=64  10 02 00 01 01 05 10 03 04 00 ...
✅  SUCCESS — bidirectional HID communication working!
```

---

## Option B — Production / App Store distribution

1. Sign in to developer.apple.com and request the DriverKit entitlements listed
   in `DSP408Driver.entitlements` under your App ID.
2. Create a provisioning profile for the DEXT.
3. Embed the DEXT in your macOS app.
4. Activate via `OSSystemExtensionManager.shared.submitRequest(...)` at launch.
5. The user grants permission in System Settings → General → Login Items &
   Extensions.

Apple review takes ~1-2 weeks for DriverKit entitlements.

---

## How the fix works (technical detail)

IOKit applies the matched personality dictionary's properties to the service at
match time via `IOService::startCandidate()`.  Because `HIDDefaultBehavior = Yes`
is in our personality, IOKit merges it into the live `IOHIDInterface` nub's
property table **before** calling `Start()`.  `hidd` watches for
`HIDDefaultBehavior` changes and, seeing `Yes`, calls `IOHIDInterface::open()`.
This allocates delivery buffers (`CreatedBuffers > 0`) and sets
`DeviceOpenedByEventSystem = Yes`.  From that point on, every inbound USB
interrupt-IN packet flows:

```
USB EP_IN → IOUSBHostInterface → AppleUserHIDDevice
         → IOHIDInterface (buffers exist)
         → DSP408HIDDriver (DEXT, our driver)
         → hidd event system
         → IOHIDDevice callbacks in user space  ✅
```

The outbound path (SET_REPORT / EP_OUT) was already working and is unaffected.

---

## Uninstalling

```bash
sudo systemextensionsctl uninstall com.example.DSP408HIDDriver
```

The device reverts to `AppleUserHIDDrivers.dext` management with
`HIDDefaultBehavior = ""` and broken input delivery.
