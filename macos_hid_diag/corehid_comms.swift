// CoreHID DSP-408 communications test for macOS 26
import CoreHID
import Foundation

setbuf(stdout, nil)  // unbuffered output

func hex(_ d: Data, n: Int = 16) -> String {
    d.prefix(n).map { String(format: "%02x", $0) }.joined(separator: " ")
}

func buildFrame(_ cmd: UInt8) -> Data {
    let n = UInt8(1); let chk: UInt8 = n ^ cmd
    var f: [UInt8] = [0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk]
    while f.count < 64 { f.append(0) }
    return Data(f)
}

let task = Task {
    print("[1] Creating HIDDeviceManager")
    let manager = HIDDeviceManager()

    print("[2] Searching for device VID=0x0483 PID=0x5750 ...")
    let criteria = HIDDeviceManager.DeviceMatchingCriteria(vendorID: 0x0483, productID: 0x5750)

    // Find device (already matched will arrive immediately)
    var deviceRef: HIDDeviceClient.DeviceReference? = nil
    let managerStream = await manager.monitorNotifications(matchingCriteria: [criteria])
    for try await note in managerStream {
        if case .deviceMatched(let ref) = note {
            deviceRef = ref
            print("[3] Device matched: \(ref)")
            break
        }
    }
    guard let ref = deviceRef else { print("Device not found"); exit(1) }

    print("[4] Creating HIDDeviceClient ...")
    guard let client = HIDDeviceClient(deviceReference: ref) else {
        print("HIDDeviceClient init returned nil"); exit(1)
    }
    print("[5] Client created: \(client)")
    print("    product=\(await client.product ?? "?")")
    print("    vendor=\(String(format:"0x%04x", await client.vendorID)) product=\(String(format:"0x%04x", await client.productID))")
    let desc = await client.descriptor
    print("    descriptor[\(desc.count)]: \(hex(desc, n: desc.count))")

    // Subscribe to input reports (all report IDs)
    print("[6] Starting notification monitor ...")
    let notifStream = await client.monitorNotifications(
        reportIDsToMonitor: [HIDReportID.allReports],
        elementsToMonitor: []
    )

    // Send OP_INIT and wait for response
    let cmds: [(String, UInt8)] = [
        ("OP_INIT  0x10", 0x10), ("OP_FW   0x13", 0x13),
        ("OP_POLL 0x40",  0x40), ("OP_INFO 0x2C", 0x2C),
    ]

    for (label, cmd) in cmds {
        print("\n[→] \(label)")
        print("    frame: \(hex(buildFrame(cmd), n: 10))")

        do {
            try await client.dispatchSetReportRequest(
                type: .output, id: nil, data: buildFrame(cmd), timeout: .seconds(1))
            print("    dispatchSetReportRequest: OK")
        } catch {
            print("    dispatchSetReportRequest FAILED: \(error)")
            continue
        }

        // Poll for response (2s timeout)
        let deadline = ContinuousClock.now + .seconds(2)
        var gotReport = false
        while ContinuousClock.now < deadline {
            let remaining = deadline - ContinuousClock.now
            // Try to get next notification with a short timeout
            let notifTask = Task {
                for try await notif in notifStream {
                    return notif
                }
                return nil as HIDDeviceClient.Notification?
            }
            let waitTask = Task {
                try await Task.sleep(for: .milliseconds(100))
            }
            // Wait for whichever comes first
            do {
                try await waitTask.value
            } catch { }

            if !notifTask.isCancelled {
                notifTask.cancel()
            }

            // Check if we got a result
            if let result = await notifTask.value {
                switch result {
                case .inputReport(let id, let data, _):
                    print("    ← INPUT: id=\(id.map{String($0.rawValue)} ?? "nil") len=\(data.count) data=\(hex(data, n: 16))")
                    gotReport = true
                default:
                    print("    ← OTHER: \(result)")
                }
                break
            }
        }
        if !gotReport { print("    ← TIMEOUT") }
    }

    // Try seizing and resending
    print("\n[7] Attempting seizeDevice() ...")
    do {
        try await client.seizeDevice()
        print("    seizeDevice: OK")

        try await client.dispatchSetReportRequest(
            type: .output, id: nil, data: buildFrame(0x10), timeout: .seconds(1))
        print("    SetReport after seize: OK — waiting 3s for input ...")

        let deadline2 = ContinuousClock.now + .seconds(3)
        for try await notif in notifStream {
            if case .inputReport(let id, let data, _) = notif {
                print("    ← SEIZED INPUT: id=\(id.map{String($0.rawValue)} ?? "nil") data=\(hex(data))")
                break
            }
            if ContinuousClock.now > deadline2 { break }
        }
    } catch {
        print("    seizeDevice FAILED: \(error)")
    }

    print("\n[8] Done.")
    exit(0)
}

RunLoop.main.run()
