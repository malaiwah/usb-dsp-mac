#include <stdio.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

static int g_cb_count = 0;
static uint8_t g_buf[64];
static IOHIDDeviceRef g_dev = NULL;

void report_cb(void *ctx, IOReturn result, void *sender,
               IOHIDReportType type, uint32_t rid,
               uint8_t *report, CFIndex len) {
    g_cb_count++;
    printf("  [CB #%d] type=%d id=%d len=%ld buf[0]=%02x\n",
           g_cb_count, type, rid, (long)len, g_buf[0]);
    fflush(stdout);
}

void device_matched(void *ctx, IOReturn result, void *sender, IOHIDDeviceRef dev) {
    printf("Device matched: %p\n", dev);
    CFNumberRef vid = (CFNumberRef)IOHIDDeviceGetProperty(dev, CFSTR(kIOHIDVendorIDKey));
    CFNumberRef pid = (CFNumberRef)IOHIDDeviceGetProperty(dev, CFSTR(kIOHIDProductIDKey));
    int v=0, p=0;
    if (vid) CFNumberGetValue(vid, kCFNumberSInt32Type, &v);
    if (pid) CFNumberGetValue(pid, kCFNumberSInt32Type, &p);
    printf("  VID=%04x PID=%04x\n", v, p);
    
    if (v == 0x0483 && p == 0x5750) {
        g_dev = dev;
        
        IOReturn ret = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeNone);
        printf("  IOHIDDeviceOpen: %08x\n", ret);
        
        // Register input report callback
        IOHIDDeviceRegisterInputReportCallback(dev, g_buf, sizeof(g_buf),
                                               report_cb, NULL);
        printf("  Callback registered\n");
        
        // Send OP_INIT
        uint8_t frame[64] = {0x10, 0x02, 0x00, 0x01, 0x01, 0x10, 0x10, 0x03, 0x11};
        ret = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, frame, sizeof(frame));
        printf("  SetReport: %08x\n", ret);
    }
}

int main(void) {
    IOHIDManagerRef mgr = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
    printf("IOHIDManagerRef: %p\n", mgr);
    
    // Match by VID/PID
    int vid = 0x0483, pid = 0x5750;
    CFNumberRef cfVid = CFNumberCreate(NULL, kCFNumberIntType, &vid);
    CFNumberRef cfPid = CFNumberCreate(NULL, kCFNumberIntType, &pid);
    CFStringRef keys[] = { CFSTR(kIOHIDVendorIDKey), CFSTR(kIOHIDProductIDKey) };
    CFTypeRef   vals[] = { cfVid, cfPid };
    CFDictionaryRef match = CFDictionaryCreate(NULL, (const void**)keys, (const void**)vals, 2,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    
    IOHIDManagerSetDeviceMatching(mgr, match);
    CFRelease(match); CFRelease(cfVid); CFRelease(cfPid);
    
    // Schedule manager on run loop
    IOHIDManagerScheduleWithRunLoop(mgr, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    
    // Register device matched callback
    IOHIDManagerRegisterDeviceMatchingCallback(mgr, device_matched, NULL);
    
    // Open manager
    IOReturn ret = IOHIDManagerOpen(mgr, kIOHIDOptionsTypeNone);
    printf("IOHIDManagerOpen: %08x\n", ret);
    
    // Pump run loop to get device match
    printf("Waiting for device match (1s)...\n");
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, false);
    
    if (!g_dev) { printf("Device not matched!\n"); return 1; }
    
    // Wait for response
    printf("Waiting for callback (2s)...\n");
    CFAbsoluteTime deadline = CFAbsoluteTimeGetCurrent() + 2.0;
    while (CFAbsoluteTimeGetCurrent() < deadline && g_cb_count == 0) {
        SInt32 r = (SInt32)CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, true);
        if (r == kCFRunLoopRunHandledSource)
            printf("  Source handled\n");
    }
    
    printf("Done. callbacks=%d\n", g_cb_count);
    
    IOHIDManagerClose(mgr, kIOHIDOptionsTypeNone);
    CFRelease(mgr);
    return 0;
}
