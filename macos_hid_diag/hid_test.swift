import Foundation
import IOKit
import IOKit.hid

let VID = 0x0483
let PID = 0x5750
let REPORT_SZ = 64

func buildFrame(_ cmd: UInt8) -> [UInt8] {
    let n = UInt8(1)
    var chk = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < REPORT_SZ { f.append(0) }
    return f
}

let mgr = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
let matchDict = [kIOHIDVendorIDKey: VID, kIOHIDProductIDKey: PID] as CFDictionary
IOHIDManagerSetDeviceMatching(mgr, matchDict)

var openRet = IOHIDManagerOpen(mgr, IOOptionBits(kIOHIDOptionsTypeNone))
print("IOHIDManagerOpen: \(openRet == kIOReturnSuccess ? "OK" : "FAILED \(String(format:"0x%08x",UInt32(bitPattern:openRet)))")")
if openRet != kIOReturnSuccess { exit(1) }

// Give time for enumeration
IOHIDManagerScheduleWithRunLoop(mgr, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
CFRunLoopRunInMode(.defaultMode, 0.5, false)

let devSetOpt = IOHIDManagerCopyDevices(mgr)
guard let devSet = devSetOpt else {
    print("No devices found")
    exit(1)
}
let count = CFSetGetCount(devSet)
print("Found \(count) device(s)")
guard count > 0 else { exit(1) }

var devPtr: UnsafeRawPointer? = nil
CFSetGetValues(devSet, &devPtr)
let dev = Unmanaged<IOHIDDevice>.fromOpaque(devPtr!).takeUnretainedValue()

if let prod = IOHIDDeviceGetProperty(dev, kIOHIDProductKey as CFString) {
    print("Product: \(prod)")
}

let devOpenRet = IOHIDDeviceOpen(dev, IOOptionBits(kIOHIDOptionsTypeNone))
print("IOHIDDeviceOpen: \(devOpenRet == kIOReturnSuccess ? "OK" : "FAILED \(String(format:"0x%08x",UInt32(bitPattern:devOpenRet)))")")
if devOpenRet != kIOReturnSuccess { exit(1) }

IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)

var reportBuf = [UInt8](repeating: 0, count: REPORT_SZ)
IOHIDDeviceRegisterInputReportCallback(dev, &reportBuf, CFIndex(REPORT_SZ),
    { (ctx, result, sender, rtype, rid, report, rlen) in
        var hex = ""
        for i in 0..<min(Int(rlen), 16) { hex += String(format: "%02x ", report[i]) }
        print("  *** CALLBACK: len=\(rlen) \(hex)")
    }, nil)
print("Callback registered\n")

let cmds: [(String, UInt8)] = [("OP_INIT 0x10", 0x10), ("OP_FW 0x13", 0x13), ("OP_POLL 0x40", 0x40)]
for (label, cmd) in cmds {
    print("→ \(label)")
    let frame = buildFrame(cmd)
    let ret = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, CFIndex(0), frame, CFIndex(REPORT_SZ))
    if ret != kIOReturnSuccess {
        print("  SetReport FAILED: \(String(format:"0x%08x",UInt32(bitPattern:ret)))")
    } else {
        print("  SetReport OK, pumping 1.5s...")
        CFRunLoopRunInMode(.defaultMode, 1.5, false)
    }
    print()
}

IOHIDDeviceUnscheduleFromRunLoop(dev, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
IOHIDDeviceClose(dev, IOOptionBits(kIOHIDOptionsTypeNone))
print("Done.")
