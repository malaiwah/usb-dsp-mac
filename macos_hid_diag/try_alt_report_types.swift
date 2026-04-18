// try_alt_report_types.swift — exercises the documented Apple-Forum-742144
// workaround and a few related tricks on the DSP-408.
//
// Background:
//   https://developer.apple.com/forums/thread/742144 — "IOHIDDeviceSetReport
//   only works once" for vendor HID devices that don't acknowledge an Output
//   report. The fix that thread documents: send the request as
//   kIOHIDReportTypeInput instead, which "forces the device to not expect a
//   response."  We're trying every report-type / direction combination here
//   to see if any path delivers an inbound report from the DSP-408 to user
//   space WITHOUT needing a custom DEXT.
//
// Compile (from this directory):
//   swiftc -o try_alt_report_types try_alt_report_types.swift \
//       -framework IOKit -framework CoreFoundation
//   ./try_alt_report_types

import IOKit
import IOKit.hid
import CoreFoundation
import Foundation

let VID = 1155
let PID = 22352

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

func makeFrame(_ cmd: UInt8) -> [UInt8] {
    let n: UInt8 = 1
    let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < 64 { f.append(0) }
    return f
}

// ── Find the device ──────────────────────────────────────────────────────
let mgr = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
IOHIDManagerSetDeviceMatching(mgr, ["VendorID": VID, "ProductID": PID] as CFDictionary)
IOHIDManagerScheduleWithRunLoop(mgr, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
IOHIDManagerOpen(mgr, IOOptionBits(kIOHIDOptionsTypeNone))
CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.5, false)

guard let devSet = IOHIDManagerCopyDevices(mgr) as? Set<IOHIDDevice>,
      let dev = devSet.first else {
    log("Device not found"); exit(1)
}
log("Found IOHIDDevice")

let openR = IOHIDDeviceOpen(dev, IOOptionBits(kIOHIDOptionsTypeSeizeDevice))
log("IOHIDDeviceOpen(seize): 0x\(String(UInt32(bitPattern: openR), radix: 16))\n")

// ── Set up an input-report callback so we can detect any delivery ────────
let rxBuf = UnsafeMutablePointer<UInt8>.allocate(capacity: 64)
rxBuf.initialize(repeating: 0, count: 64)
defer { rxBuf.deallocate() }
let rxCount = UnsafeMutablePointer<Int>.allocate(capacity: 1)
rxCount.initialize(to: 0)
defer { rxCount.deallocate() }

let cb: IOHIDReportCallback = { ctx, _, _, _, _, report, len in
    let c = ctx!.assumingMemoryBound(to: Int.self); c.pointee += 1
    let hex = (0..<min(Int(len), 16)).map { String(format: "%02x", report[$0]) }.joined(separator: " ")
    let msg = "    *** RX #\(c.pointee) len=\(len)  \(hex) ...\n"
    FileHandle.standardOutput.write(msg.data(using: .utf8)!)
}
IOHIDDeviceRegisterInputReportCallback(dev, rxBuf, 64, cb, rxCount)
IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)

// ── Helper: send a frame as a given report type and pump the runloop ────
func tryReport(typeName: String, type: IOHIDReportType, cmd: UInt8 = 0x05) {
    log("─── \(typeName) (cmd=0x\(String(cmd, radix: 16))) ───")
    var frame = makeFrame(cmd)
    let before = rxCount.pointee
    let r = IOHIDDeviceSetReport(dev, type, 0, &frame, 64)
    log("  IOHIDDeviceSetReport: 0x\(String(UInt32(bitPattern: r), radix: 16))")
    CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 1.0, false)
    let delta = rxCount.pointee - before
    log("  Callbacks during 1s window: \(delta)")
    log("")
}

// ── Helper: try GetReport for each report type ──────────────────────────
func tryGetReport(typeName: String, type: IOHIDReportType) {
    log("─── GetReport \(typeName) ───")
    var buf = [UInt8](repeating: 0, count: 64)
    var len: CFIndex = 64
    let r = buf.withUnsafeMutableBufferPointer { ptr in
        IOHIDDeviceGetReport(dev, type, 0, ptr.baseAddress!, &len)
    }
    log("  IOHIDDeviceGetReport: 0x\(String(UInt32(bitPattern: r), radix: 16))  len=\(len)")
    if r == kIOReturnSuccess && len > 0 {
        let hex = (0..<min(Int(len), 16)).map { String(format: "%02x", buf[$0]) }.joined(separator: " ")
        log("  Bytes: \(hex)")
    }
    log("")
}

// ── Run the matrix ──────────────────────────────────────────────────────
log("=== Variant 1: SetReport with kIOHIDReportTypeOutput (current behavior) ===")
tryReport(typeName: "SetReport Output",  type: kIOHIDReportTypeOutput)

log("=== Variant 2: SetReport with kIOHIDReportTypeInput  (Apple Forum #742144 workaround) ===")
tryReport(typeName: "SetReport Input",   type: kIOHIDReportTypeInput)

log("=== Variant 3: SetReport with kIOHIDReportTypeFeature ===")
tryReport(typeName: "SetReport Feature", type: kIOHIDReportTypeFeature)

log("=== Variant 4: GetReport probes (does the device support feature reads?) ===")
tryGetReport(typeName: "Input",   type: kIOHIDReportTypeInput)
tryGetReport(typeName: "Output",  type: kIOHIDReportTypeOutput)
tryGetReport(typeName: "Feature", type: kIOHIDReportTypeFeature)

// One more idle window — sometimes reports trickle in late
log("=== Final 2s idle window ===")
let before = rxCount.pointee
CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 2.0, false)
log("  Callbacks during idle: \(rxCount.pointee - before)")

log("\n=== Total callbacks across all variants: \(rxCount.pointee) ===")
if rxCount.pointee > 0 {
    log("✓✓✓ At least one variant triggered an input report — investigate which one.")
} else {
    log("✗ No variant produced input reports. Confirms the DEXT/firmware path is needed.")
}

IOHIDDeviceClose(dev, IOOptionBits(kIOHIDOptionsTypeNone))
