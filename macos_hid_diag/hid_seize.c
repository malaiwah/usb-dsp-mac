#include <stdio.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

static int g_cb = 0;
static uint8_t g_buf[64];

void cb(void *ctx, IOReturn result, void *sender, IOHIDReportType type,
        uint32_t rid, uint8_t *report, CFIndex len) {
    g_cb++;
    printf("  [CB #%d] len=%ld buf=%02x%02x%02x%02x\n", g_cb, (long)len,
           g_buf[0], g_buf[1], g_buf[2], g_buf[3]);
    fflush(stdout);
}

int main(void) {
    uint64_t entry_id = 4295026141ULL;
    CFDictionaryRef matching = IORegistryEntryIDMatching(entry_id);
    io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, matching);
    IOHIDDeviceRef dev = IOHIDDeviceCreate(kCFAllocatorDefault, svc);
    IOObjectRelease(svc);
    printf("dev: %p\n", dev);
    
    // Try SEIZE
    IOReturn ret = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeSeizeDevice);
    printf("IOHIDDeviceOpen(Seize): %08x (%s)\n", ret, ret==0?"OK":"FAIL");
    
    IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    IOHIDDeviceRegisterInputReportCallback(dev, g_buf, 64, cb, NULL);
    
    // Send OP_INIT
    uint8_t frame[64] = {0x10, 0x02, 0x00, 0x01, 0x01, 0x10, 0x10, 0x03, 0x11};
    ret = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, frame, 64);
    printf("SetReport: %08x\n", ret);
    
    printf("Pumping 2s...\n");
    CFAbsoluteTime t = CFAbsoluteTimeGetCurrent() + 2.0;
    int iter = 0;
    while (CFAbsoluteTimeGetCurrent() < t) {
        SInt32 r = (SInt32)CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, true);
        iter++;
        if (r == kCFRunLoopRunHandledSource) printf("  iter %d: source handled\n", iter);
        if (r == kCFRunLoopRunFinished) printf("  iter %d: FINISHED\n", iter);
        if (g_cb) break;
    }
    printf("iter=%d cb=%d\n", iter, g_cb);
    
    IOHIDDeviceClose(dev, kIOHIDOptionsTypeNone);
    CFRelease(dev);
    return 0;
}
