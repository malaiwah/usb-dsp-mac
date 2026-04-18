// Minimal CoreHID test — check if Task runs at all
import CoreHID
import Foundation

func log(_ s: String) {
    var stdout = FileHandle.standardOutput
    stdout.write((s + "\n").data(using: .utf8)!)
}

log("TOP LEVEL — before Task")

let task = Task {
    log("INSIDE TASK — start")
    let manager = HIDDeviceManager()
    log("HIDDeviceManager created")

    let criteria = HIDDeviceManager.DeviceMatchingCriteria(
        vendorID: 0x0483,
        productID: 0x5750
    )
    log("Criteria created, calling monitorNotifications...")

    let stream = await manager.monitorNotifications(matchingCriteria: [criteria])
    log("Got stream, iterating...")

    var count = 0
    for try await notification in stream {
        count += 1
        log("NOTIFICATION [\(count)]: \(notification)")
        if case .deviceMatched(let ref) = notification {
            log("MATCHED: \(ref)")
            break
        }
        if count >= 5 { break }
    }

    log("Done — exit 0")
    exit(0)
}

log("TOP LEVEL — after Task, entering RunLoop")
RunLoop.main.run()
