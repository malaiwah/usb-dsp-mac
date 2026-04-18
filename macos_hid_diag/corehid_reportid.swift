import CoreHID
import Foundation

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

// Check HIDReportID values
let allReports = HIDReportID.allReports
log("allReports lower: \(allReports.lowerBound.rawValue)")
log("allReports upper: \(allReports.upperBound.rawValue)")

let id0 = HIDReportID(rawValue: 0)
let id1 = HIDReportID(rawValue: 1)
let id255 = HIDReportID(rawValue: 255)
log("HIDReportID(rawValue: 0): \(String(describing: id0))")
log("HIDReportID(rawValue: 1): \(String(describing: id1))")
log("HIDReportID(rawValue: 255): \(String(describing: id255))")

// Does nil reportID device report fall in allReports?
// allReports covers 0...255 or 1...255?
log("allReports contains HIDReportID(0)? \(id0.map { allReports.contains($0) } ?? false)")
log("allReports contains HIDReportID(1)? \(id1.map { allReports.contains($0) } ?? false)")
