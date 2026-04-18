// search_prop.c — test IORegistryEntrySearchCFProperty on IOHIDInterface with parent traversal
// Compile: clang -o /tmp/search_prop /tmp/search_prop.c -framework IOKit -framework CoreFoundation

#include <stdio.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>

int main(void) {
    // Find the IOHIDInterface for DSP-408 (id 0x1000119a6)
    // We need to find it as child of AppleUserHIDDevice
    CFMutableDictionaryRef match = IOServiceMatching("IOHIDDevice");
    CFNumberRef vid = CFNumberCreate(NULL, kCFNumberIntType, &(int){1155});
    CFNumberRef pid = CFNumberCreate(NULL, kCFNumberIntType, &(int){22352});
    CFDictionarySetValue(match, CFSTR("VendorID"), vid);
    CFDictionarySetValue(match, CFSTR("ProductID"), pid);
    CFRelease(vid); CFRelease(pid);

    io_service_t hidDev = IOServiceGetMatchingService(kIOMainPortDefault, match);
    if (!hidDev) { printf("AppleUserHIDDevice: NOT FOUND\n"); return 1; }
    printf("AppleUserHIDDevice: 0x%x\n", hidDev);

    // Get IOHIDInterface child
    io_iterator_t iter = IO_OBJECT_NULL;
    IORegistryEntryGetChildIterator(hidDev, kIOServicePlane, &iter);
    io_service_t iface = IO_OBJECT_NULL;
    io_service_t child;
    while ((child = IOIteratorNext(iter)) != IO_OBJECT_NULL) {
        char cn[256];
        IOObjectGetClass(child, cn);
        printf("  child: %s (0x%x)\n", cn, child);
        if (strcmp(cn, "IOHIDInterface") == 0) {
            iface = child;
        } else {
            IOObjectRelease(child);
        }
    }
    IOObjectRelease(iter);

    if (!iface) { printf("IOHIDInterface: NOT FOUND\n"); IOObjectRelease(hidDev); return 1; }
    printf("IOHIDInterface: 0x%x\n", iface);

    // ── Test 1: Direct property on IOHIDInterface ──────────────────────────────
    CFStringRef key = CFSTR("HIDDefaultBehavior");
    CFTypeRef val1 = IORegistryEntryCreateCFProperty(iface, key, kCFAllocatorDefault, 0);
    printf("\n[1] Direct on IOHIDInterface: %s\n",
           val1 ? (CFStringGetCString(val1, (char[256]){0}, 256, kCFStringEncodingUTF8),
                   (char[256]){0}) : "(null)");
    // Use a more robust print
    if (val1) {
        CFStringRef desc = CFCopyDescription(val1);
        char buf[256];
        if (desc) { CFStringGetCString(desc, buf, 256, kCFStringEncodingUTF8); printf("  value: %s\n", buf); CFRelease(desc); }
        CFRelease(val1);
    }

    // ── Test 2: Search up parents ──────────────────────────────────────────────
    CFTypeRef val2 = IORegistryEntrySearchCFProperty(
        iface, kIOServicePlane, key, kCFAllocatorDefault,
        kIORegistryIterateRecursively | kIORegistryIterateParents);
    printf("[2] SearchCFProperty(parents): %s\n", val2 ? "found" : "(null)");
    if (val2) {
        CFStringRef desc = CFCopyDescription(val2);
        char buf[256];
        if (desc) { CFStringGetCString(desc, buf, 256, kCFStringEncodingUTF8); printf("  value: %s\n", buf); CFRelease(desc); }
        CFRelease(val2);
    }

    // ── Test 3: Search down (self only) ───────────────────────────────────────
    CFTypeRef val3 = IORegistryEntrySearchCFProperty(
        iface, kIOServicePlane, key, kCFAllocatorDefault, 0);
    printf("[3] SearchCFProperty(self): %s\n", val3 ? "found" : "(null)");
    if (val3) {
        CFStringRef desc = CFCopyDescription(val3);
        char buf[256];
        if (desc) { CFStringGetCString(desc, buf, 256, kCFStringEncodingUTF8); printf("  value: %s\n", buf); CFRelease(desc); }
        CFRelease(val3);
    }

    // ── Test 4: Direct on parent (AppleUserHIDDevice) ─────────────────────────
    CFTypeRef val4 = IORegistryEntryCreateCFProperty(hidDev, key, kCFAllocatorDefault, 0);
    printf("[4] Direct on AppleUserHIDDevice: %s\n", val4 ? "found" : "(null)");
    if (val4) {
        CFStringRef desc = CFCopyDescription(val4);
        char buf[256];
        if (desc) { CFStringGetCString(desc, buf, 256, kCFStringEncodingUTF8); printf("  value: %s\n", buf); CFRelease(desc); }
        CFRelease(val4);
    }

    // ── Test 5: Check DeviceOpenedByEventSystem ────────────────────────────────
    CFTypeRef opened = IORegistryEntryCreateCFProperty(iface, CFSTR("DeviceOpenedByEventSystem"), kCFAllocatorDefault, 0);
    printf("[5] IOHIDInterface.DeviceOpenedByEventSystem: %s\n", opened ? "found" : "(not set)");
    if (opened) { CFRelease(opened); }

    IOObjectRelease(iface);
    IOObjectRelease(hidDev);
    printf("Done.\n");
    return 0;
}
