// iokit_clean2.swift — Fixed Swift exclusivity issue, tests IOKit HID callback delivery
// Compile: swiftc -o /tmp/iokit_clean2 /tmp/iokit_clean2.swift -framework IOKit -framework CoreFoundation

import IOKit
import IOKit.hid
import CoreFoundation
import Foundation

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

func ioreg_debugstate(serviceName: String) -> String {
    var matching = IOServiceMatching(serviceName) as! [String: Any]
    matching["VendorID"] = 1155
    matching["ProductID"] = 22352
    let svc = IOServiceGetMatchingService(kIOMainPortDefault, matching as CFDictionary)
    guard svc != IO_OBJECT_NULL else { return "(not found)" }
    defer { IOObjectRelease(svc) }
    var props: Unmanaged<CFMutableDictionary>? = nil
    IORegistryEntryCreateCFProperties(svc, &props, kCFAllocatorDefault, 0)
    guard let p = props?.takeRetainedValue() as? [String: Any] else { return "(props failed)" }
    if let ds = p["DebugState"] { return "\(ds)" }
    return "(no DebugState)"
}

func iface_debugstate() -> String {
    var matching = IOServiceMatching("IOHIDDevice") as! [String: Any]
    matching["VendorID"] = 1155
    matching["ProductID"] = 22352
    let hidSvc = IOServiceGetMatchingService(kIOMainPortDefault, matching as CFDictionary)
    guard hidSvc != IO_OBJECT_NULL else { return "(not found)" }
    defer { IOObjectRelease(hidSvc) }
    var iter: io_iterator_t = IO_OBJECT_NULL
    IORegistryEntryGetChildIterator(hidSvc, kIOServicePlane, &iter)
    defer { IOObjectRelease(iter) }
    while true {
        let child = IOIteratorNext(iter)
        if child == IO_OBJECT_NULL { break }
        var cn = [CChar](repeating: 0, count: 256)
        IOObjectGetClass(child, &cn)
        if String(cString: cn) == "IOHIDInterface" {
            var props: Unmanaged<CFMutableDictionary>? = nil
            IORegistryEntryCreateCFProperties(child, &props, kCFAllocatorDefault, 0)
            IOObjectRelease(child)
            if let p = props?.takeRetainedValue() as? [String: Any], let ds = p["DebugState"] {
                return "\(ds)"
            }
            return "(no DebugState)"
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

// Use heap-allocated counter to avoid Swift exclusivity conflict
let callbackCounter = UnsafeMutablePointer<Int>.allocate(capacity: 1)
callbackCounter.initialize(to: 0)
defer { callbackCounter.deallocate() }

let reportBufStorage = UnsafeMutablePointer<UInt8>.allocate(capacity: 64)
reportBufStorage.initialize(repeating: 0, count: 64)
defer { reportBufStorage.deallocate() }

log("=== IOKit HID Clean Test v2 ===\n")

var matching = IOServiceMatching("IOHIDDevice") as! [String: Any]
matching["VendorID"] = 1155
matching["ProductID"] = 22352
let service = IOServiceGetMatchingService(kIOMainPortDefault, matching as CFDictionary)
guard service != IO_OBJECT_NULL else { log("Device not found"); exit(1) }
defer { IOObjectRelease(service) }
log("Found IOHIDDevice service: 0x\(String(service, radix: 16))")

guard let dev = IOHIDDeviceCreate(kCFAllocatorDefault, service) else {
    log("IOHIDDeviceCreate failed"); exit(1)
}
log("IOHIDDeviceRef created")
log("DebugState (before): \(ioreg_debugstate(serviceName: "IOHIDDevice"))")
log("IOHIDInterface:      \(iface_debugstate())\n")

// Open
let openResult = IOHIDDeviceOpen(dev, IOOptionBits(kIOHIDOptionsTypeNone))
log("IOHIDDeviceOpen: 0x\(String(UInt32(bitPattern: openResult), radix: 16))")
log("IOHIDInterface after open: \(iface_debugstate())\n")

// Register callback (heap counter, no Swift exclusivity issue)
let inputCallback: IOHIDReportCallback = { ctx, result, sender, reportType, reportID, report, reportLength in
    let ctr = ctx!.assumingMemoryBound(to: Int.self)
    ctr.pointee += 1
    let data = Data(bytes: report, count: Int(reportLength))
    let hex = data.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
    let msg = "*** INPUT REPORT #\(ctr.pointee): type=\(reportType) id=\(reportID) len=\(reportLength) data=\(hex)\n"
    FileHandle.standardOutput.write(msg.data(using: .utf8)!)
}

IOHIDDeviceRegisterInputReportCallback(dev, reportBufStorage, 64, inputCallback, callbackCounter)
log("Callback registered")

IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
log("Scheduled with run loop")
log("IOHIDInterface after schedule: \(iface_debugstate())\n")

// ── Test A: sync GetReport before any write ──────────────────────────────────
log("[Test A] IOHIDDeviceGetReport(input, id=0)...")
var getBuf = [UInt8](repeating: 0, count: 64)
var getLen: CFIndex = 64
let getR = IOHIDDeviceGetReport(dev, kIOHIDReportTypeInput, 0, &getBuf, &getLen)
log("  Result: 0x\(String(UInt32(bitPattern: getR), radix: 16)) len=\(getLen)")
if getR == 0 {
    log("  Data: \(getBuf.prefix(16).map{String(format:"%02x",$0)}.joined(separator:" "))")
}

// ── Test B: send OP_INIT, 3s wait ────────────────────────────────────────────
log("\n[Test B] Send OP_INIT, wait 3s...")
var frame = buildFrame(0x10)
let setR = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, &frame, CFIndex(frame.count))
log("  SetReport: 0x\(String(UInt32(bitPattern: setR), radix: 16))")

let t0 = Date()
while Date().timeIntervalSince(t0) < 3.0 {
    CFRunLoopRunInMode(.defaultMode, 0.05, true)
}
log("  Callbacks after 3s: \(callbackCounter.pointee)")
log("  AppleUserHIDDevice: \(ioreg_debugstate(serviceName: "IOHIDDevice"))")
log("  IOHIDInterface:     \(iface_debugstate())")

// ── Test C: send OP_FW after wait ─────────────────────────────────────────────
log("\n[Test C] Send OP_FW (0x13), wait 3s...")
var frame2 = buildFrame(0x13)
IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, &frame2, CFIndex(frame2.count))

let t1 = Date()
while Date().timeIntervalSince(t1) < 3.0 {
    CFRunLoopRunInMode(.defaultMode, 0.05, true)
}
log("  Callbacks total: \(callbackCounter.pointee)")

// ── Test D: sync GetReport after writes ──────────────────────────────────────
log("\n[Test D] IOHIDDeviceGetReport(input) after writes...")
var getBuf2 = [UInt8](repeating: 0, count: 64)
var getLen2: CFIndex = 64
let getR2 = IOHIDDeviceGetReport(dev, kIOHIDReportTypeInput, 0, &getBuf2, &getLen2)
log("  Result: 0x\(String(UInt32(bitPattern: getR2), radix: 16)) len=\(getLen2)")
if getR2 == 0 {
    log("  Data: \(getBuf2.prefix(16).map{String(format:"%02x",$0)}.joined(separator:" "))")
}

// ── Test E: Seize + retry ─────────────────────────────────────────────────────
log("\n[Test E] Seize device + retry...")
IOHIDDeviceUnscheduleFromRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
IOHIDDeviceClose(dev, IOOptionBits(kIOHIDOptionsTypeNone))

let openSeize = IOHIDDeviceOpen(dev, IOOptionBits(kIOHIDOptionsTypeSeizeDevice))
log("  Open(seize): 0x\(String(UInt32(bitPattern: openSeize), radix: 16))")
IOHIDDeviceRegisterInputReportCallback(dev, reportBufStorage, 64, inputCallback, callbackCounter)
IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)

var frame3 = buildFrame(0x2C)  // OP_INFO
IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, &frame3, CFIndex(frame3.count))
log("  SetReport(OP_INFO) sent, waiting 3s...")

let t2 = Date()
while Date().timeIntervalSince(t2) < 3.0 {
    CFRunLoopRunInMode(.defaultMode, 0.05, true)
}
log("  Callbacks total: \(callbackCounter.pointee)")
log("  AppleUserHIDDevice: \(ioreg_debugstate(serviceName: "IOHIDDevice"))")
log("  IOHIDInterface:     \(iface_debugstate())")

// Cleanup
IOHIDDeviceUnscheduleFromRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
IOHIDDeviceClose(dev, IOOptionBits(kIOHIDOptionsTypeSeizeDevice))

log("\nFinal callback count: \(callbackCounter.pointee)")
log("Done.")
