// Quick CoreHID test: send one command then listen for 5 seconds
// Also tries dispatchGetReportRequest and monitors elements

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
    log("Finding device...")

    var ref: HIDDeviceClient.DeviceReference? = nil
    let stream = await manager.monitorNotifications(matchingCriteria: [criteria])
    for try await n in stream {
        if case .deviceMatched(let r) = n { ref = r; break }
    }
    guard let ref else { log("not found"); exit(1) }

    guard let client = HIDDeviceClient(deviceReference: ref) else { log("client failed"); exit(1) }
    log("Client ready")

    let elements = await client.elements
    let inputElement = elements.first(where: { $0.type == .input })
    log("Input element: \(String(describing: inputElement))")

    // ── Phase 1: Listen 5s WITHOUT sending anything ──────────────────────────
    log("\nPhase 1: Listening 5s for spontaneous input reports...")
    let notifStream = await client.monitorNotifications(
        reportIDsToMonitor: [HIDReportID.allReports],
        elementsToMonitor: inputElement.map { [$0] } ?? []
    )

    var listenTask = Task {
        var count = 0
        for try await notif in notifStream {
            count += 1
            switch notif {
            case .inputReport(let id, let data, _):
                let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                log("  SPONTANEOUS inputReport id=\(id.map{String($0.rawValue)} ?? "nil") data=\(hex)")
            case .elementUpdates(let vals):
                log("  SPONTANEOUS elementUpdates: \(vals.count)")
            default:
                log("  OTHER: \(notif)")
            }
        }
        return count
    }
    try? await Task.sleep(for: .seconds(5))
    listenTask.cancel()
    let count1 = (try? await listenTask.value) ?? 0
    log("Phase 1 done: \(count1) spontaneous reports in 5s")

    // ── Phase 2: seize then GET_REPORT ──────────────────────────────────────
    log("\nPhase 2: dispatchGetReportRequest (poll for input)...")
    do {
        let data = try await client.dispatchGetReportRequest(type: .input, id: nil, timeout: .seconds(2))
        let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
        log("  GET_REPORT OK: \(data.count)B: \(hex)")
    } catch {
        log("  GET_REPORT FAILED: \(error)")
    }

    // ── Phase 3: Send OP_INIT, listen 5s ────────────────────────────────────
    log("\nPhase 3: Send OP_INIT, listen 5s...")
    let frame = buildFrame(0x10)
    log("  Frame: \(frame.prefix(12).map{String(format:"%02x",$0)}.joined(separator:" "))")
    do {
        try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("  dispatchSetReportRequest: OK")
    } catch {
        log("  dispatchSetReportRequest FAILED: \(error)")
    }

    let notifStream2 = await client.monitorNotifications(
        reportIDsToMonitor: [HIDReportID.allReports],
        elementsToMonitor: inputElement.map { [$0] } ?? []
    )
    var listenTask2 = Task {
        var count = 0
        for try await notif in notifStream2 {
            count += 1
            switch notif {
            case .inputReport(let id, let data, _):
                let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                log("  inputReport id=\(id.map{String($0.rawValue)} ?? "nil") len=\(data.count) data=\(hex)")
            case .elementUpdates(let vals):
                log("  elementUpdates: \(vals.count) items")
            default:
                log("  OTHER: \(notif)")
            }
        }
        return count
    }
    try? await Task.sleep(for: .seconds(5))
    listenTask2.cancel()
    let count3 = (try? await listenTask2.value) ?? 0
    log("Phase 3 done: \(count3) reports in 5s")

    // ── Phase 4: seize + send + GET_REPORT poll ──────────────────────────────
    log("\nPhase 4: seizeDevice + send OP_INIT + GET_REPORT poll...")
    do {
        try await client.seizeDevice()
        log("  seizeDevice: OK")

        try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("  dispatchSetReportRequest after seize: OK")

        // Try polling
        for i in 1...3 {
            try? await Task.sleep(for: .milliseconds(200))
            do {
                let data = try await client.dispatchGetReportRequest(type: .input, id: nil, timeout: .seconds(1))
                let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                log("  GET_REPORT[\(i)]: \(data.count)B: \(hex)")
            } catch {
                log("  GET_REPORT[\(i)] FAILED: \(error)")
            }
        }
    } catch {
        log("  seize/send FAILED: \(error)")
    }

    log("\nDone.")
    exit(0)
}

RunLoop.main.run()
