// iohidif_open.c — Try IOServiceOpen on IOHIDInterface directly
// Also tries to read input reports via any available mechanism
// Compile: clang -o /tmp/iohidif_open /tmp/iohidif_open.c -framework IOKit -framework CoreFoundation

#include <stdio.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>
#include <mach/mach.h>

int main(void) {
    printf("=== IOHIDInterface direct open test ===\n\n");

    // Find IOHIDInterface child of AppleUserHIDDevice for DSP-408
    CFMutableDictionaryRef match = IOServiceMatching("IOHIDDevice");
    CFNumberRef vid = CFNumberCreate(NULL, kCFNumberIntType, &(int){1155});
    CFNumberRef pid = CFNumberCreate(NULL, kCFNumberIntType, &(int){22352});
    CFDictionarySetValue(match, CFSTR("VendorID"), vid);
    CFDictionarySetValue(match, CFSTR("ProductID"), pid);
    CFRelease(vid); CFRelease(pid);
    io_service_t hidDev = IOServiceGetMatchingService(kIOMainPortDefault, match);
    if (!hidDev) { printf("AppleUserHIDDevice not found\n"); return 1; }

    io_iterator_t iter = IO_OBJECT_NULL;
    IORegistryEntryGetChildIterator(hidDev, kIOServicePlane, &iter);
    io_service_t iface = IO_OBJECT_NULL;
    io_service_t child;
    while ((child = IOIteratorNext(iter)) != IO_OBJECT_NULL) {
        char cn[256]; IOObjectGetClass(child, cn);
        if (strcmp(cn, "IOHIDInterface") == 0) { iface = child; }
        else IOObjectRelease(child);
    }
    IOObjectRelease(iter);

    if (!iface) { printf("IOHIDInterface not found\n"); IOObjectRelease(hidDev); return 1; }
    printf("Found IOHIDInterface: 0x%x\n", iface);

    // ── Test 1: IOServiceOpen type 0 ──────────────────────────────────────────
    io_connect_t conn0 = IO_OBJECT_NULL;
    kern_return_t kr = IOServiceOpen(iface, mach_task_self(), 0, &conn0);
    printf("[1] IOServiceOpen(type=0): 0x%x (%s) conn=0x%x\n", kr, kr==0?"OK":"FAIL", conn0);
    if (conn0) IOServiceClose(conn0);

    // ── Test 2: IOServiceOpen type 1 ──────────────────────────────────────────
    io_connect_t conn1 = IO_OBJECT_NULL;
    kr = IOServiceOpen(iface, mach_task_self(), 1, &conn1);
    printf("[2] IOServiceOpen(type=1): 0x%x (%s) conn=0x%x\n", kr, kr==0?"OK":"FAIL", conn1);
    if (conn1) IOServiceClose(conn1);

    // ── Test 3: IOServiceOpen type 2 ──────────────────────────────────────────
    io_connect_t conn2 = IO_OBJECT_NULL;
    kr = IOServiceOpen(iface, mach_task_self(), 2, &conn2);
    printf("[3] IOServiceOpen(type=2): 0x%x (%s) conn=0x%x\n", kr, kr==0?"OK":"FAIL", conn2);
    if (conn2) IOServiceClose(conn2);

    // ── Test 4: Check AppleUserHIDDevice user client type 1 ───────────────────
    io_connect_t conn3 = IO_OBJECT_NULL;
    kr = IOServiceOpen(hidDev, mach_task_self(), 1, &conn3);
    printf("[4] IOServiceOpen(AppleUserHIDDevice, type=1): 0x%x (%s)\n", kr, kr==0?"OK":"FAIL");
    if (conn3) {
        // If type 1 opens, try calling method 0 to see what it does
        uint64_t scalar_out[4]; uint32_t scalar_out_cnt = 4;
        uint64_t scalar_in[2] = {0, 0}; uint32_t scalar_in_cnt = 2;
        kern_return_t r2 = IOConnectCallScalarMethod(conn3, 0, scalar_in, scalar_in_cnt, scalar_out, &scalar_out_cnt);
        printf("  Method 0 result: 0x%x\n", r2);
        IOServiceClose(conn3);
    }

    // ── Test 5: Check IOUSBHostInterface open ─────────────────────────────────
    // Can we get a user client on the IOUSBHostInterface parent?
    io_service_t usbIface = IO_OBJECT_NULL;
    io_iterator_t piter = IO_OBJECT_NULL;
    IORegistryEntryGetParentIterator(hidDev, kIOServicePlane, &piter);
    io_service_t parent;
    while ((parent = IOIteratorNext(piter)) != IO_OBJECT_NULL) {
        char cn[256]; IOObjectGetClass(parent, cn);
        printf("[5] parent: %s (0x%x)\n", cn, parent);
        if (strcmp(cn, "IOUSBHostInterface") == 0) {
            usbIface = parent;
            // Try opening
            io_connect_t usbConn = IO_OBJECT_NULL;
            kern_return_t r = IOServiceOpen(usbIface, mach_task_self(), 0, &usbConn);
            printf("  IOServiceOpen(IOUSBHostInterface, 0): 0x%x (%s)\n", r, r==0?"OK":"FAIL");
            if (usbConn) IOServiceClose(usbConn);
        } else {
            IOObjectRelease(parent);
        }
    }
    IOObjectRelease(piter);
    if (usbIface) IOObjectRelease(usbIface);

    IOObjectRelease(iface);
    IOObjectRelease(hidDev);
    printf("Done.\n");
    return 0;
}
