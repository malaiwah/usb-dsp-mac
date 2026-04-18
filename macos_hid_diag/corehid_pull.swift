// Test pull-based approach: updateElements(RequestElementUpdate) after sending command
// Also tests monitorNotifications on a timer in the background

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

    let elements = await client.elements
    let inputEls = elements.filter { $0.type == .input }
    log("Input elements: \(inputEls.count)")
    for el in inputEls {
        log("  \(el)")
    }

    // ── Test 1: updateElements pull (pollDevice: false) ───────────────────────
    log("\n[Test 1] updateElements pull (pollDevice: false) after sending OP_INIT")
    let frame = buildFrame(0x10)

    do {
        try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("  SetReport: OK")
    } catch {
        log("  SetReport FAILED: \(error)")
    }

    // Small delay for response to arrive
    try? await Task.sleep(for: .milliseconds(100))

    let req = HIDDeviceClient.RequestElementUpdate(elements: inputEls, pollDevice: false)
    let result = await client.updateElements([req], timeout: .seconds(2))
    let vals = result[req]
    switch vals {
    case .success(let values):
        log("  updateElements success: \(values.count) values")
        for v in values {
            log("    element: \(v.element) bytes: \(v.bytes.prefix(16).map{String(format:"%02x",$0)}.joined(separator:" "))")
        }
    case .failure(let err):
        log("  updateElements FAILED: \(err)")
    case nil:
        log("  updateElements: nil result (request not in result?)")
    }

    // ── Test 2: updateElements pull (pollDevice: true) ─────────────────────────
    log("\n[Test 2] updateElements pull (pollDevice: true)")
    let req2 = HIDDeviceClient.RequestElementUpdate(elements: inputEls, pollDevice: true)
    let result2 = await client.updateElements([req2], timeout: .seconds(2))
    let vals2 = result2[req2]
    switch vals2 {
    case .success(let values):
        log("  updateElements(pollDevice:true) success: \(values.count) values")
        for v in values {
            log("    bytes: \(v.bytes.prefix(16).map{String(format:"%02x",$0)}.joined(separator:" "))")
        }
    case .failure(let err):
        log("  updateElements(pollDevice:true) FAILED: \(err)")
    case nil:
        log("  updateElements(pollDevice:true): nil result")
    }

    // ── Test 3: Monitor on background, send, wait ─────────────────────────────
    log("\n[Test 3] Start background monitoring, then send commands x5")
    // Try BOTH empty and allReports in separate streams simultaneously
    let streamA = await client.monitorNotifications(reportIDsToMonitor: [], elementsToMonitor: [])
    let streamB = await client.monitorNotifications(reportIDsToMonitor: [HIDReportID.allReports], elementsToMonitor: [])
    let streamC = await client.monitorNotifications(reportIDsToMonitor: [], elementsToMonitor: inputEls)

    let monA = Task {
        for try await n in streamA {
            log("  [StreamA] \(n)")
        }
    }
    let monB = Task {
        for try await n in streamB {
            log("  [StreamB] \(n)")
        }
    }
    let monC = Task {
        for try await n in streamC {
            log("  [StreamC] \(n)")
        }
    }

    // Brief pause to let monitors start
    try? await Task.sleep(for: .milliseconds(100))

    log("  Sending 5x OP_INIT...")
    for i in 1...5 {
        try? await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("  Sent #\(i), sleeping 500ms...")
        try? await Task.sleep(for: .milliseconds(500))
    }

    log("  Done sending, waiting 2s more...")
    try? await Task.sleep(for: .seconds(2))

    monA.cancel(); monB.cancel(); monC.cancel()
    _ = try? await monA.value; _ = try? await monB.value; _ = try? await monC.value

    log("\nTest 3 complete (any stream events logged above)")

    // ── Test 4: seize then pull ───────────────────────────────────────────────
    log("\n[Test 4] seizeDevice + updateElements pull")
    try? await client.seizeDevice()
    log("  seize: OK")

    try? await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
    log("  SetReport after seize: OK")
    try? await Task.sleep(for: .milliseconds(200))

    let req4 = HIDDeviceClient.RequestElementUpdate(elements: inputEls, pollDevice: false)
    let result4 = await client.updateElements([req4], timeout: .seconds(2))
    switch result4[req4] {
    case .success(let values):
        log("  Pull after seize: \(values.count) values")
        for v in values {
            log("    bytes: \(v.bytes.prefix(16).map{String(format:"%02x",$0)}.joined(separator:" "))")
        }
    case .failure(let err):
        log("  Pull after seize FAILED: \(err)")
    case nil:
        log("  Pull after seize: nil")
    }

    log("\nDone.")
    exit(0)
}

RunLoop.main.run()
