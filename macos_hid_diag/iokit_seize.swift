// Try old IOKit HID with kIOHIDOptionsTypeSeizeDevice flag
// AND different run loop modes/timing

import Foundation
import IOKit
import IOKit.hid

let kIOHIDOptionsTypeSeizeDevice: IOOptionBits = 1

func buildFrame(_ cmd: UInt8) -> Data {
    let n = UInt8(1)
    let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < 64 { f.append(0) }
    return Data(f)
}

// Global report buffer
var gReportCount = 0
var gReportData = Data()

print("=== IOKit HID with SeizeDevice flag ===")

let manager = IOHIDManagerCreate(kCFAllocatorDefault, 0)
let match: [String: Any] = [
    kIOHIDVendorIDKey: 0x0483,
    kIOHIDProductIDKey: 0x5750
]
IOHIDManagerSetDeviceMatching(manager, match as CFDictionary)

var hidDev: IOHIDDevice? = nil
var deviceReady = false

// Device added callback
IOHIDManagerRegisterDeviceMatchingCallback(manager, { ctx, result, sender, dev in
    print("Device matched: \(dev)")
    hidDev = dev
    deviceReady = true
}, nil)

// Schedule manager on main runloop FIRST
IOHIDManagerScheduleWithRunLoop(manager, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)

// Open manager
let openResult = IOHIDManagerOpen(manager, 0)
print("Manager open: \(openResult)")

// Wait for device to appear (up to 3s)
let deadline = Date().addingTimeInterval(3)
while !deviceReady && Date() < deadline {
    CFRunLoopRunInMode(.defaultMode, 0.1, false)
}

guard let dev = hidDev else {
    print("No device found")
    exit(1)
}
print("Got device: \(dev)")

// Open with seize flag
let devOpenResult = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeSeizeDevice)
print("IOHIDDeviceOpen(seize): \(devOpenResult)")

// Register input report callback
var reportBuffer = [UInt8](repeating: 0, count: 64)
let bufPtr = UnsafeMutableRawPointer(&reportBuffer)

let inputCallback: IOHIDReportCallback = { ctx, result, sender, reportType, reportID, report, reportLength in
    let data = Data(bytes: report, count: Int(reportLength))
    let hex = data.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
    print("*** INPUT REPORT CALLBACK! type=\(reportType) id=\(reportID) len=\(reportLength) data=\(hex)")
    gReportCount += 1
}

IOHIDDeviceRegisterInputReportCallback(dev, &reportBuffer, 64, inputCallback, nil)
IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
print("Callback registered and scheduled on main runloop")

// Also register input value callback
let valueCallback: IOHIDValueCallback = { ctx, result, sender, value in
    print("*** INPUT VALUE CALLBACK!")
    gReportCount += 1
}
IOHIDDeviceRegisterInputValueCallback(dev, valueCallback, nil)

// Send OP_INIT
let frame = buildFrame(0x10)
var frameBytes = [UInt8](frame)
let setResult = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, &frameBytes, frame.count)
print("SetReport(OP_INIT): \(setResult)")
print("Waiting 3s for callback...")

// Spin runloop for 3s
let waitDeadline = Date().addingTimeInterval(3)
while Date() < waitDeadline {
    CFRunLoopRunInMode(.defaultMode, 0.1, false)
}

print("Result: \(gReportCount) callbacks in 3s")

// Try more commands with longer wait
let cmds: [(String, UInt8)] = [
    ("OP_FW 0x13", 0x13),
    ("OP_INFO 0x2C", 0x2C),
]
for (label, cmd) in cmds {
    let f = buildFrame(cmd)
    var fb = [UInt8](f)
    let r = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, &fb, f.count)
    print("SetReport(\(label)): \(r), waiting 2s...")
    let w = Date().addingTimeInterval(2)
    while Date() < w { CFRunLoopRunInMode(.defaultMode, 0.05, false) }
}

print("Total callbacks: \(gReportCount)")
print("Done")
exit(0)
