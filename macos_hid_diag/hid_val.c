#include <stdio.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <IOKit/hid/IOHIDValue.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

static int g_val_cb = 0;
static int g_rep_cb = 0;
static uint8_t g_buf[64];

void report_cb(void *ctx, IOReturn result, void *sender, IOHIDReportType type,
               uint32_t rid, uint8_t *report, CFIndex len) {
    g_rep_cb++;
    printf("  [REPORT CB #%d] len=%ld\n", g_rep_cb, (long)len);
    fflush(stdout);
}

void value_cb(void *ctx, IOReturn result, void *sender, IOHIDValueRef value) {
    g_val_cb++;
    IOHIDElementRef elem = IOHIDValueGetElement(value);
    printf("  [VALUE CB #%d] element=%p\n", g_val_cb, elem);
    fflush(stdout);
}

void device_cb(void *ctx, IOReturn result, void *sender, IOHIDDeviceRef dev) {
    printf("Device matched: %p\n", dev);
    
    IOReturn ret = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeSeizeDevice);
    printf("  Open: %08x\n", ret);
    
    // Schedule both callbacks
    IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    IOHIDDeviceRegisterInputReportCallback(dev, g_buf, 64, report_cb, NULL);
    IOHIDDeviceRegisterInputValueCallback(dev, value_cb, NULL);
    
    // Send frame
    uint8_t frame[64] = {0x10, 0x02, 0x00, 0x01, 0x01, 0x10, 0x10, 0x03, 0x11};
    ret = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, frame, 64);
    printf("  SetReport: %08x\n", ret);
}

int main(void) {
    IOHIDManagerRef mgr = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
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
    IOHIDManagerOpen(mgr, kIOHIDOptionsTypeNone);
    
    printf("Waiting 3s...\n");
    CFAbsoluteTime t = CFAbsoluteTimeGetCurrent() + 3.0;
    int iter = 0;
    while (CFAbsoluteTimeGetCurrent() < t) {
        SInt32 r = (SInt32)CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, true);
        iter++;
        if (r == kCFRunLoopRunHandledSource) printf("  [%d] source!\n", iter);
        if (g_rep_cb > 0 || g_val_cb > 0) break;
    }
    printf("iter=%d report_cb=%d value_cb=%d\n", iter, g_rep_cb, g_val_cb);
    return 0;
}
