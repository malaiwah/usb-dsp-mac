#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

static int g_callback_count = 0;
static uint8_t g_report_buf[64];

void input_report_callback(void *context, IOReturn result, void *sender,
                            IOHIDReportType type, uint32_t reportID,
                            uint8_t *report, CFIndex reportLength)
{
    g_callback_count++;
    printf("  [CALLBACK #%d] type=%d id=%d len=%ld data=", 
           g_callback_count, type, reportID, (long)reportLength);
    for (CFIndex i = 0; i < (reportLength < 8 ? reportLength : 8); i++)
        printf("%02x", g_report_buf[i]);
    printf("\n");
    fflush(stdout);
}

int main(void) {
    uint64_t entry_id = 4295026141ULL;
    
    CFDictionaryRef matching = IORegistryEntryIDMatching(entry_id);
    if (!matching) { fprintf(stderr, "IORegistryEntryIDMatching failed\n"); return 1; }
    
    io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, matching);
    printf("service_t: 0x%08x\n", service);
    if (!service) { fprintf(stderr, "Service not found\n"); return 1; }
    
    IOHIDDeviceRef dev = IOHIDDeviceCreate(kCFAllocatorDefault, service);
    IOObjectRelease(service);
    printf("IOHIDDeviceRef: %p\n", dev);
    if (!dev) { fprintf(stderr, "IOHIDDeviceCreate failed\n"); return 1; }
    
    IOReturn ret = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeNone);
    printf("IOHIDDeviceOpen: 0x%08x (%s)\n", ret, ret == kIOReturnSuccess ? "OK" : "FAIL");
    if (ret != kIOReturnSuccess) { CFRelease(dev); return 1; }
    
    CFRunLoopRef rl = CFRunLoopGetCurrent();
    IOHIDDeviceScheduleWithRunLoop(dev, rl, kCFRunLoopDefaultMode);
    printf("Scheduled with run loop\n");
    
    IOHIDDeviceRegisterInputReportCallback(dev, g_report_buf, sizeof(g_report_buf),
                                           input_report_callback, NULL);
    printf("Callback registered\n");
    
    // Send OP_INIT frame
    uint8_t frame[64] = {0x10, 0x02, 0x00, 0x01, 0x01, 0x10, 0x10, 0x03, 0x11};
    ret = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0, frame, sizeof(frame));
    printf("IOHIDDeviceSetReport: 0x%08x\n", ret);
    
    // Pump run loop for 2 seconds
    printf("Pumping run loop for 2 seconds...\n");
    int iter = 0;
    CFAbsoluteTime deadline = CFAbsoluteTimeGetCurrent() + 2.0;
    while (CFAbsoluteTimeGetCurrent() < deadline) {
        SInt32 result = (SInt32)CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, true);
        iter++;
        if (result == kCFRunLoopRunHandledSource) {
            printf("  [iter %d] source handled!\n", iter);
        } else if (result == kCFRunLoopRunFinished) {
            printf("  [iter %d] run loop finished (no sources)!\n", iter);
        }
        if (g_callback_count > 0) break;
    }
    
    printf("Done. iter=%d callbacks=%d\n", iter, g_callback_count);
    
    IOHIDDeviceUnscheduleFromRunLoop(dev, rl, kCFRunLoopDefaultMode);
    IOHIDDeviceClose(dev, kIOHIDOptionsTypeNone);
    CFRelease(dev);
    return 0;
}
