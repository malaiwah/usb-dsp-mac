// CoreHID full comms test for DSP-408
// Uses FileHandle.standardOutput for unbuffered logging

import CoreHID
import Foundation

let VID: UInt32 = 0x0483
let PID: UInt32 = 0x5750
let REPORT_SZ = 64

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

func buildFrame(_ cmd: UInt8) -> Data {
    let n = UInt8(1)
    let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < REPORT_SZ { f.append(0) }
    return Data(f)
}

let task = Task {
    let manager = HIDDeviceManager()
    let criteria = HIDDeviceManager.DeviceMatchingCriteria(
        vendorID: VID,
        productID: PID
    )

    log("Searching for DSP-408 (VID=\(String(format:"0x%04x",VID)) PID=\(String(format:"0x%04x",PID)))...")

    var deviceRef: HIDDeviceClient.DeviceReference? = nil
    let stream = await manager.monitorNotifications(matchingCriteria: [criteria])
    for try await notification in stream {
        if case .deviceMatched(let ref) = notification {
            deviceRef = ref
            log("Found device: \(ref)")
            break
        }
    }

    guard let ref = deviceRef else {
        log("Device not found")
        exit(1)
    }

    guard let client = HIDDeviceClient(deviceReference: ref) else {
        log("HIDDeviceClient init failed")
        exit(1)
    }

    log("HIDDeviceClient created")
    let prod = await client.product ?? "(nil)"
    let mfr  = await client.manufacturer ?? "(nil)"
    log("  Product: \(prod)")
    log("  Manufacturer: \(mfr)")
    log("  VID: \(String(format:"0x%04x", await client.vendorID))")
    log("  PID: \(String(format:"0x%04x", await client.productID))")

    let descData = await client.descriptor
    log("  Report descriptor (\(descData.count)B): \(descData.map{String(format:"%02x",$0)}.joined())")

    let elements = await client.elements
    log("  Elements: \(elements.count)")
    for el in elements {
        log("    \(el)")
    }

    // Start monitoring input notifications BEFORE sending commands
    let notifStream = await client.monitorNotifications(
        reportIDsToMonitor: [HIDReportID.allReports],
        elementsToMonitor: []
    )

    log("\n--- Sending commands ---")

    let cmds: [(String, UInt8)] = [
        ("OP_INIT  0x10", 0x10),
        ("OP_FW   0x13",  0x13),
        ("OP_INFO 0x2C",  0x2C),
        ("OP_POLL 0x40",  0x40),
    ]

    for (label, cmd) in cmds {
        log("\n→ \(label)")
        let frame = buildFrame(cmd)
        log("  Frame: \(frame.prefix(12).map{String(format:"%02x",$0)}.joined(separator:" "))")

        do {
            try await client.dispatchSetReportRequest(
                type: .output,
                id: nil,
                data: frame,
                timeout: .seconds(1)
            )
            log("  dispatchSetReportRequest: OK")
        } catch {
            log("  dispatchSetReportRequest FAILED: \(error)")
            continue
        }

        // Wait up to 2 seconds for input notification
        log("  Waiting 2s for input report...")

        // Use a cancellable sub-task for the timeout
        let receiveTask = Task {
            for try await notif in notifStream {
                switch notif {
                case .inputReport(let id, let data, _):
                    let hex = data.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
                    let idStr = id.map { String($0.rawValue) } ?? "nil"
                    log("  ← INPUT REPORT: id=\(idStr) len=\(data.count) data=\(hex)")
                    return true
                case .elementUpdates(let values):
                    log("  ← ELEMENT UPDATE: \(values.count) values")
                case .deviceSeized:
                    log("  ← DEVICE SEIZED")
                case .deviceUnseized:
                    log("  ← DEVICE UNSEIZED")
                case .deviceRemoved:
                    log("  ← DEVICE REMOVED")
                    exit(0)
                @unknown default:
                    log("  ← UNKNOWN NOTIFICATION")
                }
            }
            return false
        }

        // Wait 2s then cancel if no report
        try? await Task.sleep(for: .seconds(2))
        let gotReport = receiveTask.isCancelled ? false : await { () async -> Bool in
            receiveTask.cancel()
            return (try? await receiveTask.value) ?? false
        }()

        if !gotReport {
            log("  ← TIMEOUT (no input report in 2s)")
        }
    }

    // Try with seize
    log("\n--- Trying seizeDevice() ---")
    do {
        try await client.seizeDevice()
        log("seizeDevice: OK — re-sending OP_INIT")

        let frame = buildFrame(0x10)
        try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("SetReport after seize: OK, waiting 2s...")

        let seizeTask = Task {
            for try await notif in notifStream {
                if case .inputReport(let id, let data, _) = notif {
                    let hex = data.prefix(16).map { String(format: "%02x", $0) }.joined(separator: " ")
                    let idStr = id.map { String($0.rawValue) } ?? "nil"
                    log("← POST-SEIZE INPUT: id=\(idStr) data=\(hex)")
                    return
                }
            }
        }
        try? await Task.sleep(for: .seconds(2))
        seizeTask.cancel()
        _ = try? await seizeTask.value
    } catch {
        log("seizeDevice FAILED: \(error)")
    }

    log("\nDone.")
    exit(0)
}

RunLoop.main.run()
