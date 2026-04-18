// dext_activate_host.swift — Minimal host app to activate DSP408HIDDriver.dext
// via OSSystemExtensionManager (the proper API for non-SIP-disabled machines).
//
// Compile (from this directory):
//   swiftc -o dext_activate dext_activate_host.swift \
//       -framework SystemExtensions -framework Foundation
//
// This only works if:
//   1. DSP408HIDDriver.dext is embedded in the app bundle at
//      Contents/Library/SystemExtensions/DSP408HIDDriver.dext
//   2. The host app is signed with the matching entitlements
//   3. You have the required DriverKit entitlements (approved by Apple)
//
// For development/SIP-disabled testing, use:
//   sudo systemextensionsctl install /path/to/DSP408HIDDriver.dext
// instead of this script.

import Foundation
import SystemExtensions

class ActivationDelegate: NSObject, OSSystemExtensionRequestDelegate {
    func request(_ request: OSSystemExtensionRequest,
                 actionForReplacingExtension existing: OSSystemExtensionProperties,
                 withExtension ext: OSSystemExtensionProperties) -> OSSystemExtensionRequest.ReplacementAction {
        print("Replacing existing version \(existing.bundleShortVersion) → \(ext.bundleShortVersion)")
        return .replace
    }

    func requestNeedsUserApproval(_ request: OSSystemExtensionRequest) {
        print("⚠️  User approval required in System Settings → Privacy & Security")
        print("    (or General → Login Items & Extensions on newer macOS)")
    }

    func request(_ request: OSSystemExtensionRequest,
                 didFinishWithResult result: OSSystemExtensionRequest.Result) {
        switch result {
        case .completed:
            print("✅  Extension activated — DSP408HIDDriver.dext is running")
        case .willCompleteAfterReboot:
            print("⚠️  Will complete after reboot")
        @unknown default:
            print("Unknown result: \(result)")
        }
        CFRunLoopStop(CFRunLoopGetMain())
    }

    func request(_ request: OSSystemExtensionRequest, didFailWithError error: Error) {
        print("❌  Activation failed: \(error)")
        CFRunLoopStop(CFRunLoopGetMain())
    }
}

let delegate = ActivationDelegate()
let req = OSSystemExtensionRequest.activationRequest(
    forExtensionWithIdentifier: "com.example.DSP408HIDDriver",
    queue: .main)
req.delegate = delegate
OSSystemExtensionManager.shared.submitRequest(req)

print("Submitted activation request for com.example.DSP408HIDDriver …")
CFRunLoopRun()
