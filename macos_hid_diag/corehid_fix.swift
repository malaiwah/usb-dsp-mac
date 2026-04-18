// THE FIX: reportIDsToMonitor: [] (empty) for nil-reportID devices
// HIDReportID.allReports = 1...255 — doesn't include nil (no-report-ID) devices!

import CoreHID
import Foundation

func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

func buildFrame(_ cmd: UInt8) -> Data {
    let n = UInt8(1)
    let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < 64 { f.append(0) }
    return Data(f)
}

let task = Task {
    let manager = HIDDeviceManager()
    let criteria = HIDDeviceManager.DeviceMatchingCriteria(vendorID: 0x0483, productID: 0x5750)

    log("Finding DSP-408...")
    var ref: HIDDeviceClient.DeviceReference? = nil
    for try await n in await manager.monitorNotifications(matchingCriteria: [criteria]) {
        if case .deviceMatched(let r) = n { ref = r; break }
    }
    guard let ref else { log("not found"); exit(1) }
    guard let client = HIDDeviceClient(deviceReference: ref) else { log("client failed"); exit(1) }
    log("Client ready: \(ref)")

    // ── Subscribe BEFORE seize, with EMPTY reportIDsToMonitor ──────────────────
    // Empty [] means: monitor ALL reports, including nil-ID (no-report-ID) reports
    log("\nSetting up monitorNotifications with reportIDsToMonitor: [] ...")
    let notifStream = await client.monitorNotifications(
        reportIDsToMonitor: [],    // <-- THE FIX: not [HIDReportID.allReports]
        elementsToMonitor: []
    )
    log("Stream ready. Sending OP_INIT...")

    let frame = buildFrame(0x10)
    log("Frame: \(frame.prefix(12).map{String(format:"%02x",$0)}.joined(separator:" "))")

    do {
        try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("dispatchSetReportRequest: OK")
    } catch {
        log("dispatchSetReportRequest FAILED: \(error)")
    }

    log("Listening 5s for input reports...")
    let listenTask = Task {
        var count = 0
        for try await notif in notifStream {
            count += 1
            switch notif {
            case .inputReport(let id, let data, _):
                let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                log("← INPUT REPORT #\(count): id=\(id.map{String($0.rawValue)} ?? "nil") len=\(data.count) data=\(hex)")
                return count
            case .elementUpdates(let vals):
                log("← ELEMENT UPDATE #\(count): \(vals.count) values")
            case .deviceSeized:
                log("← DEVICE SEIZED by other")
            case .deviceUnseized:
                log("← DEVICE UNSEIZED")
            case .deviceRemoved:
                log("← DEVICE REMOVED")
                exit(0)
            @unknown default:
                log("← UNKNOWN #\(count)")
            }
        }
        return count
    }
    try? await Task.sleep(for: .seconds(5))
    listenTask.cancel()
    let got = (try? await listenTask.value) ?? 0
    log("Result: \(got) reports in 5s")

    if got == 0 {
        // Try with seize
        log("\nNo reports without seize. Trying seize + [] filter...")
        try? await client.seizeDevice()
        log("seizeDevice: OK")

        let notifStream2 = await client.monitorNotifications(
            reportIDsToMonitor: [],
            elementsToMonitor: []
        )

        try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("SetReport sent. Listening 5s...")

        let lt2 = Task {
            var count = 0
            for try await notif in notifStream2 {
                count += 1
                if case .inputReport(let id, let data, _) = notif {
                    let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                    log("← POST-SEIZE INPUT #\(count): id=\(id.map{String($0.rawValue)} ?? "nil") data=\(hex)")
                    return count
                } else {
                    log("← other: \(notif)")
                }
            }
            return count
        }
        try? await Task.sleep(for: .seconds(5))
        lt2.cancel()
        let got2 = (try? await lt2.value) ?? 0
        log("Post-seize result: \(got2) reports in 5s")
    }

    // Send all 4 commands and listen
    log("\n--- All 4 commands ---")
    let cmds: [(String, UInt8)] = [
        ("OP_INIT 0x10", 0x10),
        ("OP_FW   0x13", 0x13),
        ("OP_INFO 0x2C", 0x2C),
        ("OP_POLL 0x40", 0x40),
    ]

    for (label, cmd) in cmds {
        log("\n→ \(label)")
        let f = buildFrame(cmd)
        do {
            try await client.dispatchSetReportRequest(type: .output, id: nil, data: f, timeout: .seconds(1))
            log("  SetReport OK")
        } catch {
            log("  SetReport FAILED: \(error)")
            continue
        }

        let notifStream3 = await client.monitorNotifications(reportIDsToMonitor: [], elementsToMonitor: [])
        let lt3 = Task {
            for try await notif in notifStream3 {
                if case .inputReport(let id, let data, _) = notif {
                    let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                    log("  ← INPUT: id=\(id.map{String($0.rawValue)} ?? "nil") data=\(hex)")
                    return
                }
            }
        }
        try? await Task.sleep(for: .seconds(3))
        lt3.cancel()
        try? await lt3.value
    }

    log("\nDone.")
    exit(0)
}

RunLoop.main.run()
