#include <stdio.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

static int g_cb = 0;
static uint8_t g_buf[64];
static IOHIDDeviceRef g_dev = NULL;

// Manager-level input report callback (different from device-level)
void mgr_report_cb(void *ctx, IOReturn result, void *sender,
                   IOHIDReportType type, uint32_t rid,
                   uint8_t *report, CFIndex len) {
    g_cb++;
    printf("  [MGR REPORT CB #%d] sender=%p type=%d len=%ld buf=%02x%02x\n",
           g_cb, sender, type, (long)len, g_buf[0], g_buf[1]);
    fflush(stdout);
}

void device_cb(void *ctx, IOReturn result, void *sender, IOHIDDeviceRef dev) {
    CFNumberRef vid = (CFNumberRef)IOHIDDeviceGetProperty(dev, CFSTR(kIOHIDVendorIDKey));
    int v=0; if (vid) CFNumberGetValue(vid, kCFNumberSInt32Type, &v);
    if (v != 0x0483) return;
    
    printf("DSP-408 matched: %p\n", dev);
    g_dev = dev;
}

int main(void) {
    IOHIDManagerRef mgr = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
    
    // Match our device
    int vid=0x0483, pid=0x5750;
    CFNumberRef cfV=CFNumberCreate(NULL,kCFNumberIntType,&vid);
    CFNumberRef cfP=CFNumberCreate(NULL,kCFNumberIntType,&pid);
    CFStringRef keys[]={CFSTR(kIOHIDVendorIDKey),CFSTR(kIOHIDProductIDKey)};
    CFTypeRef vals[]={cfV,cfP};
    CFDictionaryRef m=CFDictionaryCreate(NULL,(const void**)keys,(const void**)vals,2,
        &kCFTypeDictionaryKeyCallBacks,&kCFTypeDictionaryValueCallBacks);
    IOHIDManagerSetDeviceMatching(mgr, m);
    CFRelease(m); CFRelease(cfV); CFRelease(cfP);
    
    IOHIDManagerScheduleWithRunLoop(mgr, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    IOHIDManagerRegisterDeviceMatchingCallback(mgr, device_cb, NULL);
    
    // Register MANAGER-LEVEL input report callback (before open)
    IOHIDManagerRegisterInputReportCallback(mgr, g_buf, sizeof(g_buf),
                                             mgr_report_cb, NULL);
    printf("Manager callback registered\n");
    
    // Open manager (opens devices in non-exclusive mode)
    IOReturn ret = IOHIDManagerOpen(mgr, kIOHIDOptionsTypeNone);
    printf("IOHIDManagerOpen: %08x\n", ret);
    
    // Wait for device to be matched
    printf("Waiting for match...\n");
    CFAbsoluteTime t0 = CFAbsoluteTimeGetCurrent() + 1.0;
    while (CFAbsoluteTimeGetCurrent() < t0 && !g_dev) {
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, true);
    }
    printf("g_dev = %p\n", g_dev);
    if (!g_dev) { printf("No device!\n"); return 1; }
    
    // Send via device-level API
    uint8_t frame[64] = {0x10, 0x02, 0x00, 0x01, 0x01, 0x10, 0x10, 0x03, 0x11};
    ret = IOHIDDeviceSetReport(g_dev, kIOHIDReportTypeOutput, 0, frame, 64);
    printf("SetReport: %08x\n", ret);
    
    // Wait for manager-level callback
    printf("Waiting for callback (2s)...\n");
    int iter=0;
    CFAbsoluteTime t1 = CFAbsoluteTimeGetCurrent() + 2.0;
    while (CFAbsoluteTimeGetCurrent() < t1) {
        SInt32 r = (SInt32)CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, true);
        iter++;
        if (r == kCFRunLoopRunHandledSource) printf("  [%d] source\n", iter);
        if (g_cb) break;
    }
    
    printf("Done. iter=%d cb=%d\n", iter, g_cb);
    IOHIDManagerClose(mgr, kIOHIDOptionsTypeNone);
    CFRelease(mgr);
    return 0;
}
