# macOS HID Diagnostic Tools

These are the throwaway-but-keep diagnostic programs written while figuring out
why the Dayton Audio DSP-408 (VID=0x0483 / PID=0x5750) refuses to deliver input
reports to user space on macOS 26.

The investigation pinpointed the root cause:

> `IOHIDInterface.HIDDefaultBehavior` is set to `""` (empty string) by Apple's
> generic `AppleUserHIDDrivers.dext`. With an empty string `hidd` never opens
> the IOHIDInterface, so `CreatedBuffers` stays 0 and every input-report path
> (IOHIDDevice callbacks, CoreHID `dispatchSetReportRequest`, etc.) is broken.
>
> `IORegistryEntrySetCFProperties` returns `kIOReturnNotPermitted (0xe00002c7)`
> for **all** IOHIDInterface services from any user-space caller regardless of
> privilege. The only supported way to fix it is via an IOKitPersonalities
> entry in a matched DriverKit driver (see `../macos_hid_driver/`).

## Most useful files

| File | What it proved |
|------|----------------|
| `force_hid_prop.c` | `IORegistryEntrySetCFProperties` succeeds on `AppleUserHIDDevice` (parent) but returns `kIOReturnNotPermitted` for **every** `IOHIDInterface` system-wide — confirms the block is in the `IOHIDFamily` kext, not a privilege check |
| `search_prop.c` | `IORegistryEntrySearchCFProperty` with parent traversal finds the empty `""` on `IOHIDInterface` itself **before** reaching the parent's `"Yes"`, so the parent property never wins |
| `iohidif_open.c` | Tests `IOServiceOpen` on `IOHIDInterface` (all types fail with `0xe00002c7`), `AppleUserHIDDevice` type 1 (`0xe00002bc`), and `IOUSBHostInterface` (succeeds but pipes are exclusive) |
| `usb_direct.swift` | All `IOConnectCallScalarMethod` calls on `IOUSBHostInterface` return `kIOReturnExclusiveAccess (0xe00002c2)` — DriverKit owns the pipes |
| `hid_svc_client.c` | `IOHIDEventSystemClientCopyServices()` returns 121 services, DSP-408 is **not** among them — the device isn't in the event system's managed set |
| `iokit_clean2.swift` | Definitive proof: `IOHIDDeviceOpen()` returns 0, `IOHIDDeviceScheduleWithRunLoop()` is called, but **0 callbacks** ever fire because `IOHIDInterface.CreatedBuffers = 0` |
| `corehid_fix.swift` | CoreHID test using `dispatchSetReportRequest` with seize — confirms outbound works (`InputReportCount` increments at USB level) but inbound delivery is dead |
| `verify_fix.sh` | Quick post-fix verification: checks `HIDDefaultBehavior`, `DeviceOpenedByEventSystem`, `CreatedBuffers`, `hidutil list` |
| `watch_ioreg.sh` | Watches `IORegistry` `DebugState` counters live to see whether USB-level reception is happening |

## Build commands

Most C files compile with:
```bash
clang -o force_hid_prop force_hid_prop.c -framework IOKit -framework CoreFoundation
```

Most Swift files compile with:
```bash
swiftc -o iokit_clean2 iokit_clean2.swift -framework IOKit -framework CoreFoundation
```

The `corehid_*.swift` files require the private CoreHID framework — usually
linked with `-F /System/Library/PrivateFrameworks -framework HID`.

## Key error codes encountered

| Hex | Symbol | Meaning |
|-----|--------|---------|
| `0xe00002c7` | `kIOReturnNotPermitted` | `IOHIDInterface::setProperties()` blocks the call |
| `0xe00002c2` | `kIOReturnExclusiveAccess` | DriverKit holds USB pipes |
| `0xe00002bc` | `kIOReturnError` | Generic; here = AppleUserHIDDevice type-1 user client unavailable |
| `0xe0005000` | (HID transport) | Device doesn't support HID GET_REPORT control request |

## See also

- `../macos_hid_driver/` — the actual DEXT that fixes the bug
- `../MACOS_USB_FIX.md` — earlier write-up of attempted fixes
