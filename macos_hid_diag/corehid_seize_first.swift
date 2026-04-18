// CoreHID: seize FIRST, then monitorNotifications, then send commands
// Key insight: InputReportCount=17 in IORegistry but CoreHID delivers 0
// Fix hypothesis: seize before subscribing to notifications

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

// Read InputReportCount from shell so we can compare before/after
func ioregInputReportCount() -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = ["-c",
        "ioreg -l -w 0 2>/dev/null | grep DebugState | grep -o 'InputReportCount=[0-9]*'"]
    let pipe = Pipe()
    p.standardOutput = pipe
    try? p.run(); p.waitUntilExit()
    return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? "(error)"
}

let task = Task {
    let manager = HIDDeviceManager()
    let criteria = HIDDeviceManager.DeviceMatchingCriteria(vendorID: 0x0483, productID: 0x5750)

    log("Finding device...")
    var ref: HIDDeviceClient.DeviceReference? = nil
    for try await n in await manager.monitorNotifications(matchingCriteria: [criteria]) {
        if case .deviceMatched(let r) = n { ref = r; break }
    }
    guard let ref else { log("not found"); exit(1) }
    guard let client = HIDDeviceClient(deviceReference: ref) else { log("client failed"); exit(1) }
    log("Client ready (deviceID: \(ref))")

    // ── STEP 1: Seize device FIRST ────────────────────────────────────────────
    log("\n[Step 1] Seizing device...")
    do {
        try await client.seizeDevice()
        log("  seizeDevice: OK")
    } catch {
        log("  seizeDevice FAILED: \(error) — continuing anyway")
    }

    // ── STEP 2: Set up notifications AFTER seize ─────────────────────────────
    log("\n[Step 2] Setting up monitorNotifications AFTER seize...")
    let notifStream = await client.monitorNotifications(
        reportIDsToMonitor: [HIDReportID.allReports],
        elementsToMonitor: []
    )
    log("  Stream ready")

    // ── STEP 3: Send commands and listen ─────────────────────────────────────
    let cmds: [(String, UInt8)] = [
        ("OP_INIT 0x10", 0x10),
        ("OP_FW   0x13", 0x13),
        ("OP_INFO 0x2C", 0x2C),
        ("OP_POLL 0x40", 0x40),
    ]

    for (label, cmd) in cmds {
        log("\n→ \(label)")
        let before = ioregInputReportCount()

        let frame = buildFrame(cmd)
        do {
            try await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
            log("  SetReport: OK")
        } catch {
            log("  SetReport FAILED: \(error)")
            continue
        }

        // Listen for 3 seconds
        log("  Listening 3s... (IORegistry before: \(before))")
        let listenTask = Task {
            var count = 0
            for try await notif in notifStream {
                count += 1
                switch notif {
                case .inputReport(let id, let data, _):
                    let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                    log("  ← INPUT REPORT #\(count): id=\(id.map{String($0.rawValue)} ?? "nil") len=\(data.count) \(hex)")
                    return count
                case .elementUpdates(let vals):
                    log("  ← ELEMENT UPDATE #\(count): \(vals.count) items")
                case .deviceSeized:
                    log("  ← DEVICE SEIZED by other")
                case .deviceUnseized:
                    log("  ← DEVICE UNSEIZED")
                case .deviceRemoved:
                    log("  ← DEVICE REMOVED")
                    exit(0)
                @unknown default:
                    log("  ← UNKNOWN")
                }
            }
            return count
        }
        try? await Task.sleep(for: .seconds(3))
        listenTask.cancel()
        let got = (try? await listenTask.value) ?? 0
        let after = ioregInputReportCount()
        log("  Result: \(got) reports delivered to CoreHID (IORegistry after: \(after))")
    }

    // ── STEP 4: Try with seize + element monitoring ───────────────────────────
    log("\n[Step 4] Try monitorNotifications with elementsToMonitor...")
    let elements = await client.elements
    let inputEls = elements.filter { $0.type == .input }
    log("  Input elements: \(inputEls.count)")

    if !inputEls.isEmpty {
        let elStream = await client.monitorNotifications(
            reportIDsToMonitor: [],
            elementsToMonitor: inputEls
        )

        let frame = buildFrame(0x10)
        try? await client.dispatchSetReportRequest(type: .output, id: nil, data: frame, timeout: .seconds(1))
        log("  SetReport sent, listening 3s for element updates...")

        let elTask = Task {
            var count = 0
            for try await notif in elStream {
                count += 1
                switch notif {
                case .inputReport(let id, let data, _):
                    let hex = data.prefix(16).map { String(format:"%02x",$0) }.joined(separator:" ")
                    log("  ← inputReport #\(count): id=\(id.map{String($0.rawValue)} ?? "nil") \(hex)")
                case .elementUpdates(let vals):
                    log("  ← elementUpdates #\(count): \(vals.count) items")
                    for v in vals { log("    value: \(v)") }
                    return count
                default:
                    log("  ← other #\(count)")
                }
            }
            return count
        }
        try? await Task.sleep(for: .seconds(3))
        elTask.cancel()
        let got = (try? await elTask.value) ?? 0
        log("  Element monitoring: \(got) notifications")
    }

    log("\n=== Final IORegistry ===")
    log(ioregInputReportCount())
    log("\nDone.")
    exit(0)
}

RunLoop.main.run()
