import Foundation

let errVal: Int32 = -536850432
let uval = UInt32(bitPattern: errVal)
let system = (uval >> 26) & 0x3f
let sub = (uval >> 14) & 0xfff
let code = uval & 0x3fff

print(String(format: "Error: 0x%08X = %d", uval, errVal))
print(String(format: "system: 0x%02X (%s)", system, system == 0x38 ? "IOKit" : "other"))
print(String(format: "sub: 0x%03X (%s)", sub, sub == 1 ? "USB" : sub == 0 ? "common" : "other"))
print(String(format: "code: 0x%04X = %d", code, code))

// USB-specific: kIOUSBHostReturnPipeStalled
let kPipeStalled: UInt32 = 0xE0005000
print("\n0xE0005000 is our error? \(kPipeStalled == uval)")
