// DSP408HIDDriver.cpp — Implementation of the DSP-408 HID event driver.
//
// WHY THIS EXISTS
// ---------------
// macOS's generic DriverKit HID driver (AppleUserHIDDrivers.dext) sets
// HIDDefaultBehavior="" on every IOHIDInterface it creates for unrecognised
// USB HID devices (usage page 0 / usage 0 like the DSP-408).  With an empty
// string hidd never calls IOHIDInterface::open(), so CreatedBuffers stays 0
// and ALL input-report delivery paths (IOHIDDevice callbacks, CoreHID
// dispatchSetReportRequest, etc.) are permanently broken.
//
// This driver matches IOHIDInterface for VID=0x0483/PID=0x5750 with a high
// IOProbeScore, wins the match contest, and calls super::Start() which:
//   1. Opens the IOHIDInterface provider  → CreatedBuffers > 0
//   2. Registers a report handler         → InputReportCount increments *and*
//      reports reach user space
//   3. Sets DeviceOpenedByEventSystem=Yes in the registry
//
// After this driver loads, the existing user-space tools
// (corehid_fix, iokit_clean2, etc. in ../macos_hid_diag/) will receive
// reports normally.
//
// BUILD (requires macOS 12+ SDK and Xcode 13+)
// ---
// See BUILD.md in this directory.

#include <os/log.h>
#include <HIDDriverKit/HIDDriverKit.h>
#include "DSP408HIDDriver.h"

#define LOG(fmt, ...)  os_log(OS_LOG_DEFAULT, "[DSP408] " fmt, ##__VA_ARGS__)
#define LOGD(fmt, ...) os_log_debug(OS_LOG_DEFAULT, "[DSP408] " fmt, ##__VA_ARGS__)

#undef super
#define super IOUserHIDEventDriver
OSDefineMetaClassAndStructors(DSP408HIDDriver, IOUserHIDEventDriver)

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
kern_return_t IMPL(DSP408HIDDriver, Start)
{
    LOG("Start — matching IOHIDInterface for DSP-408");

    kern_return_t ret = Start(provider, SUPERDISPATCH);
    if (ret != kIOReturnSuccess) {
        LOG("super::Start FAILED: 0x%x — driver will not load", ret);
        return ret;
    }

    LOG("Start OK — IOHIDInterface is open, CreatedBuffers > 0, reports will flow");
    return kIOReturnSuccess;
}

// ---------------------------------------------------------------------------
// Stop
// ---------------------------------------------------------------------------
kern_return_t IMPL(DSP408HIDDriver, Stop)
{
    LOG("Stop");
    return Stop(provider, SUPERDISPATCH);
}

// ---------------------------------------------------------------------------
// handleReport  (LOCALONLY — runs in the DriverKit process, not in the kernel)
// ---------------------------------------------------------------------------
// Only override this if you need raw access to every inbound report inside the
// DEXT process itself (e.g. to push data through a custom XPC service).
// Calling SUPERDISPATCH is mandatory — it forwards the report to hidd so that
// IOHIDDevice callbacks in ordinary apps fire correctly.
void IMPL(DSP408HIDDriver, handleReport)
{
    // Log the first few bytes so we can verify reports arrive.
    if (reportLength >= 4) {
        LOGD("report type=%u id=%u len=%u  %02x %02x %02x %02x …",
             type, reportID, reportLength,
             report[0], report[1], report[2], report[3]);
    } else {
        LOGD("report type=%u id=%u len=%u", type, reportID, reportLength);
    }

    // MUST call super — this delivers the report to the HID event system,
    // which is what makes IOHIDDevice input-report callbacks fire in user space.
    handleReport(timestamp, report, reportLength, type, reportID, SUPERDISPATCH);
}
