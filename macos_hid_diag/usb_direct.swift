// usb_direct.swift — direct USB access via IOUSBHost framework
// Even though DriverKit owns the interface, IOServiceOpen succeeded.
// Let's try to access EP_IN (0x82) directly.
// Compile: swiftc -o /tmp/usb_direct /tmp/usb_direct.swift -framework IOKit -framework CoreFoundation

import IOKit
import CoreFoundation
import Foundation

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

func buildFrame(_ cmd: UInt8) -> [UInt8] {
    let n = UInt8(1); let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < 64 { f.append(0) }; return f
}

log("=== Direct USB IOUSBHostInterface test ===")

// 1. Find IOUSBHostInterface for DSP-408
// It's the parent of AppleUserHIDDevice
var matching = IOServiceMatching("IOHIDDevice") as! [String: Any]
matching["VendorID"] = 1155
matching["ProductID"] = 22352
let hidDev = IOServiceGetMatchingService(kIOMainPortDefault, matching as CFDictionary)
guard hidDev != IO_OBJECT_NULL else { log("HID device not found"); exit(1) }
defer { IOObjectRelease(hidDev) }

// Find the IOUSBHostInterface parent
var pIter: io_iterator_t = IO_OBJECT_NULL
IORegistryEntryGetParentIterator(hidDev, kIOServicePlane, &pIter)
var usbIface: io_service_t = IO_OBJECT_NULL
while true {
    let p = IOIteratorNext(pIter)
    if p == IO_OBJECT_NULL { break }
    var cn = [CChar](repeating: 0, count: 256)
    IOObjectGetClass(p, &cn)
    if String(cString: cn) == "IOUSBHostInterface" { usbIface = p; break }
    IOObjectRelease(p)
}
IOObjectRelease(pIter)
guard usbIface != IO_OBJECT_NULL else { log("IOUSBHostInterface not found"); exit(1) }
defer { IOObjectRelease(usbIface) }
log("Found IOUSBHostInterface: 0x\(String(usbIface, radix: 16))")

// 2. Open user client connection
var conn: io_connect_t = IO_OBJECT_NULL
let openKR = IOServiceOpen(usbIface, mach_task_self_, 0, &conn)
log("IOServiceOpen: 0x\(String(UInt32(bitPattern: openKR), radix: 16)) conn=0x\(String(conn, radix: 16))")
guard openKR == KERN_SUCCESS, conn != IO_OBJECT_NULL else { log("Open failed"); exit(1) }
defer { IOServiceClose(conn) }

// 3. Try to get pipe handle for EP_IN (0x82) and EP_OUT (0x01)
// IOUSBHostInterfaceUserClient methods are private, but we can try calling method 0
// Method numbers (approximate from IOUSBHostInterfaceUserClient):
// 0: Open
// 1: Close
// 2: GetPipeStatus
// 3: AbortPipe
// 4: ResetPipe
// 5: ClearPipeStall
// ...

log("\nTrying IOConnectCallMethod calls on IOUSBHostInterface connection...")

// Try method 0 (might be "open interface")
var scalarIn = [UInt64](repeating: 0, count: 8)
var scalarOut = [UInt64](repeating: 0, count: 8)
var scalarOutCnt: UInt32 = 8

let kr0 = IOConnectCallScalarMethod(conn, 0, &scalarIn, 0, &scalarOut, &scalarOutCnt)
log("Method 0: 0x\(String(UInt32(bitPattern: kr0), radix: 16)) scalarOut=\(scalarOut.prefix(Int(scalarOutCnt)))")

// Try method 1
scalarOutCnt = 8
let kr1 = IOConnectCallScalarMethod(conn, 1, &scalarIn, 0, &scalarOut, &scalarOutCnt)
log("Method 1: 0x\(String(UInt32(bitPattern: kr1), radix: 16)) scalarOut=\(scalarOut.prefix(Int(scalarOutCnt)))")

// Try calling with pipe address 0x82 (EP_IN) - endpointAddress in scalarIn[0]
scalarIn[0] = 0x82 // EP_IN address
scalarOutCnt = 8
let kr2 = IOConnectCallScalarMethod(conn, 2, &scalarIn, 1, &scalarOut, &scalarOutCnt)
log("Method 2 (pipeAddr=0x82): 0x\(String(UInt32(bitPattern: kr2), radix: 16)) out=\(scalarOut.prefix(Int(scalarOutCnt)))")

// Try method 3 with EP_OUT (0x01)
scalarIn[0] = 0x01
scalarOutCnt = 8
let kr3 = IOConnectCallScalarMethod(conn, 3, &scalarIn, 1, &scalarOut, &scalarOutCnt)
log("Method 3 (pipeAddr=0x01): 0x\(String(UInt32(bitPattern: kr3), radix: 16)) out=\(scalarOut.prefix(Int(scalarOutCnt)))")

// 4. Try alternate approach: use IOUSBHostInterface via the IOKit method dispatch
// to get pipe reference and do async read on EP_IN
// We need to find the right method selectors

// Let's also check the struct/device interface plugin
// The CFPlugIn approach for IOUSBHostInterface gives us a C++ vtable

// Log the interface properties
log("\nIOUSBHostInterface properties:")
var props: Unmanaged<CFMutableDictionary>? = nil
IORegistryEntryCreateCFProperties(usbIface, &props, kCFAllocatorDefault, 0)
if let p = props?.takeRetainedValue() as? [String: Any] {
    for key in ["bInterfaceNumber", "bNumEndpoints", "UsbExclusiveOwner", "bEndpointAddress", "idVendor", "idProduct"] {
        if let v = p[key] { log("  \(key) = \(v)") }
    }
}

// 5. Try to look at pipe objects via IORegistry children
log("\nIOUSBHostInterface children:")
var cIter: io_iterator_t = IO_OBJECT_NULL
IORegistryEntryGetChildIterator(usbIface, kIOServicePlane, &cIter)
while true {
    let c = IOIteratorNext(cIter)
    if c == IO_OBJECT_NULL { break }
    var cn = [CChar](repeating: 0, count: 256)
    IOObjectGetClass(c, &cn)
    var cprops: Unmanaged<CFMutableDictionary>? = nil
    IORegistryEntryCreateCFProperties(c, &cprops, kCFAllocatorDefault, 0)
    var epAddr = "(none)"
    if let cp = cprops?.takeRetainedValue() as? [String: Any], let ep = cp["bEndpointAddress"] {
        epAddr = "ep=0x\(String(Int(ep as? Int ?? 0), radix: 16))"
    }
    log("  \(String(cString: cn)) 0x\(String(c, radix: 16)) \(epAddr)")
    IOObjectRelease(c)
}
IOObjectRelease(cIter)

log("\nDone.")
