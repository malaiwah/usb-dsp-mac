import CoreHID
import Foundation

// Force stdout unbuffered
setbuf(stdout, nil)

print("Starting CoreHID device search...")
fflush(stdout)

let task = Task {
    let manager = HIDDeviceManager()
    print("HIDDeviceManager created")
    fflush(stdout)

    // Try matching all USB HID devices
    let criteria = HIDDeviceManager.DeviceMatchingCriteria(
        vendorID: 0x0483,
        productID: 0x5750
    )
    let criteriaAll = HIDDeviceManager.DeviceMatchingCriteria()  // match everything

    // Try broad match first
    print("Starting monitorNotifications (5s timeout)...")
    fflush(stdout)

    // Wrap in a task with timeout
    let searchTask = Task {
        var count = 0
        for try await notification in await manager.monitorNotifications(matchingCriteria: [criteria]) {
            count += 1
            print("NOTIFICATION [\(count)]: \(notification)")
            fflush(stdout)
            if count >= 5 { break }
        }
        print("Stream ended. Received \(count) notifications.")
    }

    // Kill after 5 seconds
    try await Task.sleep(for: .seconds(5))
    searchTask.cancel()
    print("Timeout reached.")
    fflush(stdout)
    exit(0)
}

RunLoop.main.run()
