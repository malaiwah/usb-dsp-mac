// iokit_clean.swift — Clean IOKit HID test (no IOHIDManager, direct service open)
// Tests: open device, register callback, send report, check DebugState changes
// Compile: swiftc -o /tmp/iokit_clean /tmp/iokit_clean.swift -framework IOKit -framework CoreFoundation

import IOKit
import IOKit.hid
import CoreFoundation
import Foundation

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

func ioreg_debugstate() -> String {
    // Read DebugState from AppleUserHIDDevice
    var matching = IOServiceMatching("IOHIDDevice") as! [String: Any]
    matching["VendorID"] = 1155
    matching["ProductID"] = 22352
    let svc = IOServiceGetMatchingService(kIOMainPortDefault, matching as CFDictionary)
    guard svc != IO_OBJECT_NULL else { return "(device not found)" }
    defer { IOObjectRelease(svc) }

    var props: Unmanaged<CFMutableDictionary>? = nil
    let kr = IORegistryEntryCreateCFProperties(svc, &props, kCFAllocatorDefault, 0)
    guard kr == KERN_SUCCESS, let p = props?.takeRetainedValue() as? [String: Any] else {
        return "(props failed: \(kr))"
    }
    if let ds = p["DebugState"] { return "\(ds)" }
    return "(no DebugState)"
}

func iface_debugstate() -> String {
    // Read DebugState from IOHIDInterface child of AppleUserHIDDevice
    var matching = IOServiceMatching("IOHIDDevice") as! [String: Any]
    matching["VendorID"] = 1155
    matching["ProductID"] = 22352
    let hidSvc = IOServiceGetMatchingService(kIOMainPortDefault, matching as CFDictionary)
    guard hidSvc != IO_OBJECT_NULL else { return "(device not found)" }
    defer { IOObjectRelease(hidSvc) }

    var iter: io_iterator_t = IO_OBJECT_NULL
    IORegistryEntryGetChildIterator(hidSvc, kIOServicePlane, &iter)
    defer { IOObjectRelease(iter) }

    var child: io_service_t
    while true {
        child = IOIteratorNext(iter)
        if child == IO_OBJECT_NULL { break }
        var cn = [CChar](repeating: 0, count: 256)
        IOObjectGetClass(child, &cn)
        if String(cString: cn) == "IOHIDInterface" {
            var props: Unmanaged<CFMutableDictionary>? = nil
            IORegistryEntryCreateCFProperties(child, &props, kCFAllocatorDefault, 0)
            IOObjectRelease(child)
            if let p = props?.takeRetainedValue() as? [String: Any],
               let ds = p["DebugState"] {
                return "\(ds)"
            }
            return "(no DebugState on IOHIDInterface)"
        }
        IOObjectRelease(child)
    }
    return "(IOHIDInterface not found)"
}

func buildFrame(_ cmd: UInt8) -> [UInt8] {
    let n = UInt8(1)
    let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < 64 { f.append(0) }
    return f
}

log("=== IOKit HID Clean Test ===\n")

// ── Step 1: Find service directly ────────────────────────────────────────────
var matching = IOServiceMatching("IOHIDDevice") as! [String: Any]
matching["VendorID"] = 1155
matching["ProductID"] = 22352
let service = IOServiceGetMatchingService(kIOMainPortDefault, matching as CFDictionary)
if service == IO_OBJECT_NULL {
    log("Device service not found"); exit(1)
}
defer { IOObjectRelease(service) }
log("Found IOHIDDevice service: 0x\(String(service, radix: 16))")

// ── Step 2: Create IOHIDDeviceRef from service ────────────────────────────────
guard let dev = IOHIDDeviceCreate(kCFAllocatorDefault, service) else {
    log("IOHIDDeviceCreate failed"); exit(1)
}
log("IOHIDDeviceRef created")

log("\nDebugState (before open):")
log("  AppleUserHIDDevice: \(ioreg_debugstate())")
log("  IOHIDInterface:     \(iface_debugstate())")

// ── Step 3: Open (no seize) ───────────────────────────────────────────────────
let openResult = IOHIDDeviceOpen(dev, IOOptionBits(kIOHIDOptionsTypeNone))
log("\nIOHIDDeviceOpen: 0x\(String(openResult, radix: 16)) (\(openResult == 0 ? "OK" : "FAILED"))")

log("DebugState (after open):")
log("  IOHIDInterface: \(iface_debugstate())")

// ── Step 4: Register input report callback ────────────────────────────────────
var callbackCount = 0
var reportBuf = [UInt8](repeating: 0, count: 64)
let bufPtr = UnsafeMutablePointer<UInt8>.allocate(capacity: 64)
bufPtr.initialize(repeating: 0, count: 64)

let inputCallback: IOHIDReportCallback = { ctx, result, sender, reportType, reportID, report, reportLength in
    let count = ctx!.load(as: Int.self)
    let data = Data(bytes: report, count: Int(reportLength))
    let hex = data.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
    FileHandle.standardOutput.write(("*** INPUT REPORT #\(count+1): len=\(reportLength) data=\(hex)\n").data(using: .utf8)!)
    ctx!.storeBytes(of: count + 1, as: Int.self)
}

withUnsafeMutablePointer(to: &callbackCount) { ctxPtr in
    IOHIDDeviceRegisterInputReportCallback(dev, bufPtr, 64, inputCallback, ctxPtr)
    log("Input report callback registered")

    // ── Step 5: Schedule with run loop ────────────────────────────────────────
    IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
    log("Scheduled with run loop")

    log("DebugState (after schedule):")
    log("  IOHIDInterface: \(iface_debugstate())")

    // ── Step 6: Try synchronous GetReport ────────────────────────────────────
    log("\n[Test A] Synchronous IOHIDDeviceGetReport (type=input, id=0)...")
    var getReportBuf = [UInt8](repeating: 0, count: 64)
    var getReportLen: CFIndex = 64
    let getResult = IOHIDDeviceGetReport(dev, kIOHIDReportTypeInput, 0, &getReportBuf, &getReportLen)
    log("  Result: 0x\(String(UInt32(bitPattern: getResult), radix: 16)) len=\(getReportLen)")
    if getResult == 0 {
        let hex = getReportBuf.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
        log("  Data: \(hex)")
    }

    // ── Step 7: Send OP_INIT and wait 3s ─────────────────────────────────────
    log("\n[Test B] Send OP_INIT, wait 3s for async callback...")
    var frame = buildFrame(0x10)
    let setResult = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, &frame, CFIndex(frame.count))
    log("  SetReport: 0x\(String(UInt32(bitPattern: setResult), radix: 16))")

    log("  DebugState (after SetReport):")
    log("    AppleUserHIDDevice: \(ioreg_debugstate())")
    log("    IOHIDInterface:     \(iface_debugstate())")

    let deadline = Date().addingTimeInterval(3)
    while Date() < deadline {
        CFRunLoopRunInMode(.defaultMode, 0.1, true)
    }
    log("  Callbacks received: \(callbackCount)")

    // ── Step 8: Try GetReport AFTER SetReport ─────────────────────────────────
    log("\n[Test C] Synchronous GetReport AFTER device responded...")
    var getReportBuf2 = [UInt8](repeating: 0, count: 64)
    var getReportLen2: CFIndex = 64
    let getResult2 = IOHIDDeviceGetReport(dev, kIOHIDReportTypeInput, 0, &getReportBuf2, &getReportLen2)
    log("  Result: 0x\(String(UInt32(bitPattern: getResult2), radix: 16)) len=\(getReportLen2)")
    if getResult2 == 0 {
        let hex = getReportBuf2.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
        log("  Data: \(hex)")
    }

    // ── Step 9: Try with seize open ───────────────────────────────────────────
    log("\n[Test D] Close + reopen with seize, send again...")
    IOHIDDeviceUnscheduleFromRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
    IOHIDDeviceClose(dev, IOOptionBits(kIOHIDOptionsTypeNone))

    let openSeize = IOHIDDeviceOpen(dev, IOOptionBits(kIOHIDOptionsTypeSeizeDevice))
    log("  Open(seize): 0x\(String(UInt32(bitPattern: openSeize), radix: 16))")
    IOHIDDeviceRegisterInputReportCallback(dev, bufPtr, 64, inputCallback, ctxPtr)
    IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)

    var frame2 = buildFrame(0x13)  // OP_FW
    IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, &frame2, CFIndex(frame2.count))
    log("  SetReport(OP_FW) sent. Waiting 3s...")

    let d2 = Date().addingTimeInterval(3)
    while Date() < d2 {
        CFRunLoopRunInMode(.defaultMode, 0.1, true)
    }
    log("  Callbacks received (total): \(callbackCount)")
    log("  IOHIDInterface: \(iface_debugstate())")

    IOHIDDeviceUnscheduleFromRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
    IOHIDDeviceClose(dev, IOOptionBits(kIOHIDOptionsTypeSeizeDevice))
}

bufPtr.deallocate()
log("\nDone.")
