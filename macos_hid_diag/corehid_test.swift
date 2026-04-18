// CoreHID test for DSP-408 on macOS 26
// CoreHID is macOS 15+ Swift framework replacing IOKit HID for DriverKit devices

import CoreHID
import Foundation

let VID: UInt32 = 0x0483
let PID: UInt32 = 0x5750
let REPORT_SZ = 64

func buildFrame(_ cmd: UInt8) -> Data {
    let n = UInt8(1)
    let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < REPORT_SZ { f.append(0) }
    return Data(f)
}

// Swift 6 strict concurrency: wrap everything in a Task
let task = Task {
    let manager = HIDDeviceManager()
    let criteria = HIDDeviceManager.DeviceMatchingCriteria(
        vendorID: VID,
        productID: PID
    )

    print("Searching for DSP-408 (VID=\(String(format:"0x%04x",VID)) PID=\(String(format:"0x%04x",PID)))...")

    // monitorNotifications returns an AsyncThrowingStream
    // We'll time out after 3 seconds if device not found
    var deviceRef: HIDDeviceClient.DeviceReference? = nil

    let stream = await manager.monitorNotifications(matchingCriteria: [criteria])
    for try await notification in stream {
        if case .deviceMatched(let ref) = notification {
            deviceRef = ref
            print("Found device: \(ref)")
            break
        }
    }

    guard let ref = deviceRef else {
        print("Device not found")
        exit(1)
    }

    guard let client = await HIDDeviceClient(deviceReference: ref) else {
        print("HIDDeviceClient init failed")
        exit(1)
    }

    print("HIDDeviceClient created")
    let prod = await client.product ?? "(nil)"
    let mfr  = await client.manufacturer ?? "(nil)"
    print("  Product: \(prod)")
    print("  Manufacturer: \(mfr)")
    print("  VID: \(String(format:"0x%04x", await client.vendorID))")
    print("  PID: \(String(format:"0x%04x", await client.productID))")

    let descData = await client.descriptor
    print("  Report descriptor (\(descData.count)B): \(descData.map{String(format:"%02x",$0)}.joined())")

    let elements = await client.elements
    print("  Elements: \(elements.count)")
    for el in elements {
        print("    \(el)")
    }

    // Monitor ALL report IDs (plus elements)
    // HIDReportID.allReports is ClosedRange<HIDReportID> covering all valid IDs
    // For device with no report IDs (id=nil), use allReports range
    let notifStream = await client.monitorNotifications(
        reportIDsToMonitor: [HIDReportID.allReports],
        elementsToMonitor: []
    )

    print("\n--- Sending commands ---")

    let cmds: [(String, UInt8)] = [
        ("OP_INIT  0x10", 0x10),
        ("OP_FW   0x13",  0x13),
        ("OP_INFO 0x2C",  0x2C),
        ("OP_POLL 0x40",  0x40),
    ]

    for (label, cmd) in cmds {
        print("\n→ \(label)")
        let frame = buildFrame(cmd)
        print("  Frame: \(frame.prefix(12).map{String(format:"%02x",$0)}.joined(separator:" "))")

        do {
            try await client.dispatchSetReportRequest(
                type: .output,
                id: nil,        // no report ID
                data: frame,
                timeout: .seconds(1)
            )
            print("  dispatchSetReportRequest: OK")
        } catch {
            print("  dispatchSetReportRequest FAILED: \(error)")
            continue
        }

        // Wait up to 2 seconds for input notification
        print("  Waiting 2s for input report...")
        let deadline = ContinuousClock.now + .seconds(2)
        var gotReport = false

        for try await notif in notifStream {
            switch notif {
            case .inputReport(let id, let data, let ts):
                let hex = data.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
                let idStr = id.map { String($0.rawValue) } ?? "nil"
                print("  ← INPUT REPORT: id=\(idStr) len=\(data.count) data=\(hex)")
                gotReport = true
                break
            case .elementUpdates(let values):
                print("  ← ELEMENT UPDATE: \(values.count) values")
            case .deviceSeized:
                print("  ← DEVICE SEIZED (by another process)")
            case .deviceUnseized:
                print("  ← DEVICE UNSEIZED")
            case .deviceRemoved:
                print("  ← DEVICE REMOVED")
                exit(0)
            }
            if gotReport || ContinuousClock.now > deadline { break }
        }

        if !gotReport {
            print("  ← TIMEOUT (no input report in 2s)")
        }
    }

    // Try with seize
    print("\n--- Trying seizeDevice() ---")
    do {
        try await client.seizeDevice()
        print("seizeDevice: OK — re-sending OP_INIT")

        let frame = buildFrame(0x10)
        try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        print("SetReport after seize: OK, waiting 2s...")

        for try await notif in notifStream {
            if case .inputReport(let id, let data, _) = notif {
                let hex = data.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
                let idStr2 = id.map { String($0.rawValue) } ?? "nil"
                print("← POST-SEIZE INPUT: id=\(idStr2) data=\(hex)")
                break
            }
            break
        }
    } catch {
        print("seizeDevice FAILED: \(error)")
    }

    print("\nDone.")
    exit(0)
}

RunLoop.main.run()
