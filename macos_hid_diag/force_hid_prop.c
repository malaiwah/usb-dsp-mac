// force_hid_prop.c — set HIDDefaultBehavior=Yes on the IOHIDInterface for the DSP-408
// Compile: clang -o /tmp/force_hid_prop /tmp/force_hid_prop.c -framework IOKit -framework CoreFoundation
// Run:     sudo /tmp/force_hid_prop

#include <stdio.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/hid/IOHIDLib.h>
#include <CoreFoundation/CoreFoundation.h>

#define VID 1155   // 0x0483
#define PID 22352  // 0x5750

static void try_set_props(io_service_t svc, const char *label) {
    CFMutableDictionaryRef props = CFDictionaryCreateMutable(
        kCFAllocatorDefault, 0,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);

    CFDictionarySetValue(props, CFSTR("HIDDefaultBehavior"), CFSTR("Yes"));

    kern_return_t kr = IORegistryEntrySetCFProperties(svc, props);
    printf("[%s] IORegistryEntrySetCFProperties: 0x%x (%s)\n",
           label, kr, kr == KERN_SUCCESS ? "OK" : "FAILED");
    CFRelease(props);

    // Read back — use CFCopyDescription to safely handle any CFType (Bool, String, etc.)
    CFMutableDictionaryRef outProps = NULL;
    IORegistryEntryCreateCFProperties(svc, &outProps, kCFAllocatorDefault, 0);
    if (outProps) {
        CFTypeRef val2 = CFDictionaryGetValue(outProps, CFSTR("HIDDefaultBehavior"));
        if (val2) {
            CFStringRef desc = CFCopyDescription(val2);
            char buf[256] = "(?)";
            if (desc) { CFStringGetCString(desc, buf, sizeof(buf), kCFStringEncodingUTF8); CFRelease(desc); }
            printf("[%s] HIDDefaultBehavior after set: %s\n", label, buf);
        } else {
            printf("[%s] HIDDefaultBehavior: (key not present)\n", label);
        }
        CFRelease(outProps);
    }
}

int main(int argc, char **argv) {
    printf("=== Force HIDDefaultBehavior on IOHIDInterface for VID=%d PID=%d ===\n", VID, PID);

    // 1) Find AppleUserHIDDevice (the IOKit bridge)
    CFMutableDictionaryRef matchHID = IOServiceMatching("IOHIDDevice");
    CFNumberRef vidNum = CFNumberCreate(NULL, kCFNumberIntType, &(int){VID});
    CFNumberRef pidNum = CFNumberCreate(NULL, kCFNumberIntType, &(int){PID});
    CFDictionarySetValue(matchHID, CFSTR("VendorID"), vidNum);
    CFDictionarySetValue(matchHID, CFSTR("ProductID"), pidNum);
    CFRelease(vidNum); CFRelease(pidNum);

    io_service_t hidDev = IOServiceGetMatchingService(kIOMainPortDefault, matchHID);
    if (!hidDev) { printf("AppleUserHIDDevice: NOT FOUND\n"); }
    else {
        printf("AppleUserHIDDevice: 0x%x\n", hidDev);
        try_set_props(hidDev, "AppleUserHIDDevice");
    }

    // 2) Find IOHIDInterface (child of AppleUserHIDDevice)
    if (hidDev) {
        io_service_t iface = IO_OBJECT_NULL;
        io_iterator_t iter = IO_OBJECT_NULL;
        IORegistryEntryGetChildIterator(hidDev, kIOServicePlane, &iter);
        io_service_t child;
        while ((child = IOIteratorNext(iter)) != IO_OBJECT_NULL) {
            CFStringRef cn = IOObjectCopyClass(child);
            char buf[128];
            if (cn) CFStringGetCString(cn, buf, sizeof(buf), kCFStringEncodingUTF8);
            printf("  Child: %s (0x%x)\n", cn ? buf : "(null)", child);
            if (cn && CFStringCompare(cn, CFSTR("IOHIDInterface"), 0) == kCFCompareEqualTo) {
                iface = child;
                if (cn) CFRelease(cn);
                break;
            }
            if (cn) CFRelease(cn);
            IOObjectRelease(child);
        }
        IOObjectRelease(iter);

        if (iface) {
            printf("IOHIDInterface: 0x%x\n", iface);
            try_set_props(iface, "IOHIDInterface");

            // Also check DebugState
            CFMutableDictionaryRef dp = NULL;
            IORegistryEntryCreateCFProperties(iface, &dp, kCFAllocatorDefault, 0);
            if (dp) {
                CFTypeRef ds = CFDictionaryGetValue(dp, CFSTR("DebugState"));
                if (ds) {
                    char buf[512];
                    CFStringRef dsStr = CFCopyDescription(ds);
                    if (dsStr) {
                        CFStringGetCString(dsStr, buf, sizeof(buf), kCFStringEncodingUTF8);
                        printf("  IOHIDInterface DebugState: %s\n", buf);
                        CFRelease(dsStr);
                    }
                }
                CFRelease(dp);
            }
            IOObjectRelease(iface);
        } else {
            printf("IOHIDInterface: NOT FOUND as child\n");
        }
        IOObjectRelease(hidDev);
    }

    // 3) Try to find IOHIDInterface directly
    CFMutableDictionaryRef matchIface = IOServiceMatching("IOHIDInterface");
    io_iterator_t iter2 = IO_OBJECT_NULL;
    IOServiceGetMatchingServices(kIOMainPortDefault, matchIface, &iter2);
    io_service_t svc;
    int count = 0;
    while ((svc = IOIteratorNext(iter2)) != IO_OBJECT_NULL) {
        // Check if this is the DSP-408's interface by checking parent
        CFMutableDictionaryRef props = NULL;
        IORegistryEntryCreateCFProperties(svc, &props, kCFAllocatorDefault, 0);
        if (props) {
            // Look for clue that this is the DSP-408
            CFTypeRef dbg = CFDictionaryGetValue(props, CFSTR("DebugState"));
            if (dbg) {
                count++;
                printf("\nIOHIDInterface #%d: 0x%x\n", count, svc);
                char buf[512];
                CFStringRef s = CFCopyDescription(dbg);
                if (s) {
                    CFStringGetCString(s, buf, sizeof(buf), kCFStringEncodingUTF8);
                    printf("  DebugState: %s\n", buf);
                    CFRelease(s);
                }
                // Try to set property on every IOHIDInterface with DebugState
                try_set_props(svc, "IOHIDInterface(direct)");
            }
            CFRelease(props);
        }
        IOObjectRelease(svc);
    }
    IOObjectRelease(iter2);

    printf("\nDone.\n");
    return 0;
}
