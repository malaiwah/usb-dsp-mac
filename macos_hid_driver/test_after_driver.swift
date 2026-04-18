// test_after_driver.swift — run this AFTER DSP408HIDDriver.dext is loaded.
//
// Compile (from this directory):
//   swiftc -o test_after_driver test_after_driver.swift \
//       -framework IOKit -framework CoreFoundation
//   ./test_after_driver
//
// It verifies CreatedBuffers > 0, then sends a DSP-408 command and waits for
// the response via IOHIDDevice input-report callbacks.

import IOKit
import IOKit.hid
import CoreFoundation
import Foundation

// ── Constants ──────────────────────────────────────────────────────────────
let VID = 1155   // 0x0483
let PID = 22352  // 0x5750

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

// Build a DSP-408 DLE/STX frame: 10 02 00 01 [N] [CMD] 10 03 [CHK], padded to 64
func makeFrame(_ cmd: UInt8) -> [UInt8] {
    let n: UInt8 = 1
    let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < 64 { f.append(0) }
    return f
}

// ── Step 1: Check IOHIDInterface.DebugState.CreatedBuffers ─────────────────
log("=== DSP-408 driver verification ===\n")

func checkRegistryState() -> (createdBuffers: Int, deviceOpened: Bool) {
    var match = IOServiceMatching("IOHIDDevice") as! [String: Any]
    match["VendorID"] = VID
    match["ProductID"] = PID
    let hidDev = IOServiceGetMatchingService(kIOMainPortDefault, match as CFDictionary)
    guard hidDev != IO_OBJECT_NULL else {
        log("AppleUserHIDDevice: NOT FOUND (driver may have replaced it with a different nub)")
        return (0, false)
    }
    defer { IOObjectRelease(hidDev) }

    var cIter: io_iterator_t = IO_OBJECT_NULL
    IORegistryEntryGetChildIterator(hidDev, kIOServicePlane, &cIter)
    defer { IOObjectRelease(cIter) }

    var createdBuffers = 0
    var deviceOpened = false

    while true {
        let child = IOIteratorNext(cIter)
        if child == IO_OBJECT_NULL { break }
        defer { IOObjectRelease(child) }

        var cn = [CChar](repeating: 0, count: 256)
        IOObjectGetClass(child, &cn)
        guard String(cString: cn) == "IOHIDInterface" else { continue }

        // DeviceOpenedByEventSystem
        if let v = IORegistryEntryCreateCFProperty(child, "DeviceOpenedByEventSystem" as CFString,
                                                   kCFAllocatorDefault, 0) {
            deviceOpened = (v as? Bool) == true || (v as? String) == "Yes"
        }

        // DebugState → CreatedBuffers
        var props: Unmanaged<CFMutableDictionary>? = nil
        IORegistryEntryCreateCFProperties(child, &props, kCFAllocatorDefault, 0)
        if let p = props?.takeRetainedValue() as? [String: Any],
           let ds = p["DebugState"] as? [String: Any],
           let cb = ds["CreatedBuffers"] as? Int {
            createdBuffers = cb
        } else if let p = props?.takeRetainedValue() as? [String: Any],
                  let dsRaw = p["DebugState"] {
            // DebugState may come as a CFString description — parse it
            let desc = String(describing: dsRaw)
            if let r = desc.range(of: "CreatedBuffers\\s*=\\s*(\\d+)",
                                  options: .regularExpression) {
                let sub = desc[r]
                if let n = sub.components(separatedBy: CharacterSet.decimalDigits.inverted)
                              .compactMap({ Int($0) }).first {
                    createdBuffers = n
                }
            }
        }
    }

    return (createdBuffers, deviceOpened)
}

let state = checkRegistryState()
log("IOHIDInterface.CreatedBuffers        = \(state.createdBuffers)")
log("IOHIDInterface.DeviceOpenedByEventSystem = \(state.deviceOpened)")

if state.createdBuffers == 0 {
    log("\n❌  CreatedBuffers is still 0 — DSP408HIDDriver.dext is NOT loaded yet.")
    log("    Check: systemextensionsctl list | grep DSP408")
    log("    And:   log stream --predicate 'subsystem==\"com.apple.iokit.IOHIDFamily\"' --level debug")
    exit(1)
}
log("\n✅  CreatedBuffers > 0 — driver is active. Testing HID callbacks...\n")

// ── Step 2: Open the device and register for input reports ─────────────────
guard let manager = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
      .takeRetainedValue() as IOHIDManager? else {
    log("IOHIDManagerCreate failed"); exit(1)
}

let matchingDict: [String: Any] = ["VendorID": VID, "ProductID": PID]
IOHIDManagerSetDeviceMatching(manager, matchingDict as CFDictionary)
IOHIDManagerScheduleWithRunLoop(manager, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone))

// Wait for device to appear
CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.5, false)

let devices = IOHIDManagerCopyDevices(manager)?.takeRetainedValue() as? Set<IOHIDDevice>
guard let dev = devices?.first else {
    log("IOHIDDevice: NOT FOUND via manager (device may need re-plug after driver load)"); exit(1)
}
log("Found IOHIDDevice: \(dev)")

let openResult = IOHIDDeviceOpen(dev, IOOptionBits(kIOHIDOptionsTypeSeizeDevice))
log("IOHIDDeviceOpen(seize): 0x\(String(UInt32(bitPattern: openResult), radix: 16))")

// Allocate report buffer
let reportBuf = UnsafeMutablePointer<UInt8>.allocate(capacity: 64)
reportBuf.initialize(repeating: 0, count: 64)
defer { reportBuf.deallocate() }

let rxCount = UnsafeMutablePointer<Int>.allocate(capacity: 1)
rxCount.initialize(to: 0)
defer { rxCount.deallocate() }

let callback: IOHIDReportCallback = { ctx, result, sender, reportType, reportID, report, reportLength in
    let counter = ctx!.assumingMemoryBound(to: Int.self)
    counter.pointee += 1
    let data = Data(bytes: report, count: min(Int(reportLength), 16))
    let hex = data.map { String(format: "%02x", $0) }.joined(separator: " ")
    let msg = "*** RX #\(counter.pointee)  type=\(reportType) id=\(reportID) len=\(reportLength)  \(hex) ...\n"
    FileHandle.standardOutput.write(msg.data(using: .utf8)!)
}

IOHIDDeviceRegisterInputReportCallback(dev, reportBuf, 64, callback, rxCount)
IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)

// ── Step 3: Send a DSP-408 command (0x05 = get firmware version) ──────────
let CMD_GET_VERSION: UInt8 = 0x05
let frame = makeFrame(CMD_GET_VERSION)

log("Sending GET_VERSION command (0x05) via IOHIDDeviceSetReport …")
var frameCopy = frame
let setResult = IOHIDDeviceSetReport(dev,
                                     kIOHIDReportTypeOutput,
                                     CFIndex(0),
                                     &frameCopy,
                                     CFIndex(64))
log("IOHIDDeviceSetReport: 0x\(String(UInt32(bitPattern: setResult), radix: 16))")

// ── Step 4: Run for 3s and count received reports ─────────────────────────
log("\nWaiting 3s for input report callbacks …")
CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 3.0, false)
log("\nCallbacks received: \(rxCount.pointee)")

if rxCount.pointee > 0 {
    log("✅  SUCCESS — bidirectional HID communication working!")
} else {
    log("❌  Still 0 callbacks. Check log stream for [DSP408] messages.")
    log("    Try: log show --last 1m --predicate 'process == \"DSP408HIDDriver\"'")
}

IOHIDDeviceClose(dev, IOOptionBits(kIOHIDOptionsTypeNone))
