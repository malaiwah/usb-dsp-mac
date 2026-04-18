// hid_svc_client.c — Use IOHIDEventSystemClient to list services and set properties
// Compile: clang -o /tmp/hid_svc_client /tmp/hid_svc_client.c -framework IOKit -framework CoreFoundation
// This tests whether the DSP-408 appears as a service and whether we can interact with it

#include <stdio.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/hidsystem/IOHIDEventSystemClient.h>
#include <IOKit/hidsystem/IOHIDServiceClient.h>
#include <CoreFoundation/CoreFoundation.h>

// Print a CFType value
static void printVal(CFTypeRef val) {
    if (!val) { printf("(null)"); return; }
    CFStringRef desc = CFCopyDescription(val);
    char buf[256];
    if (desc) { CFStringGetCString(desc, buf, 256, kCFStringEncodingUTF8); printf("%s", buf); CFRelease(desc); }
}

int main(void) {
    printf("=== IOHIDEventSystemClient test ===\n\n");

    IOHIDEventSystemClientRef client = IOHIDEventSystemClientCreateSimpleClient(kCFAllocatorDefault);
    if (!client) { printf("IOHIDEventSystemClientCreateSimpleClient FAILED\n"); return 1; }
    printf("EventSystemClient created\n");

    CFArrayRef services = IOHIDEventSystemClientCopyServices(client);
    if (!services) { printf("CopyServices returned null\n"); CFRelease(client); return 1; }
    printf("Services count: %ld\n\n", CFArrayGetCount(services));

    IOHIDServiceClientRef dspService = NULL;

    for (CFIndex i = 0; i < CFArrayGetCount(services); i++) {
        IOHIDServiceClientRef svc = (IOHIDServiceClientRef)CFArrayGetValueAtIndex(services, i);

        // Get registry ID
        CFTypeRef regID = IOHIDServiceClientGetRegistryID(svc);
        uint64_t rid = 0;
        if (regID && CFGetTypeID(regID) == CFNumberGetTypeID()) {
            CFNumberGetValue(regID, kCFNumberLongLongType, &rid);
        }

        // Get vendor/product
        CFTypeRef vid = IOHIDServiceClientCopyProperty(svc, CFSTR("VendorID"));
        CFTypeRef pid = IOHIDServiceClientCopyProperty(svc, CFSTR("ProductID"));

        int vidVal = 0, pidVal = 0;
        if (vid && CFGetTypeID(vid) == CFNumberGetTypeID()) CFNumberGetValue(vid, kCFNumberIntType, &vidVal);
        if (pid && CFGetTypeID(pid) == CFNumberGetTypeID()) CFNumberGetValue(pid, kCFNumberIntType, &pidVal);

        if (vidVal == 1155 && pidVal == 22352) {
            printf("*** FOUND DSP-408! RegistryID=0x%llx ***\n", rid);

            // Print all relevant properties
            const char *keys[] = {
                "HIDDefaultBehavior", "DeviceOpenedByEventSystem",
                "PrimaryUsagePage", "PrimaryUsage",
                "Product", "Manufacturer", "Transport",
                "IOClass", NULL
            };
            for (int k = 0; keys[k]; k++) {
                CFStringRef key = CFStringCreateWithCString(NULL, keys[k], kCFStringEncodingUTF8);
                CFTypeRef val = IOHIDServiceClientCopyProperty(svc, key);
                printf("  %s: ", keys[k]);
                printVal(val);
                printf("\n");
                if (val) CFRelease(val);
                CFRelease(key);
            }

            dspService = svc;
            CFRetain(dspService);
        }

        if (vid) CFRelease(vid);
        if (pid) CFRelease(pid);
    }

    if (!dspService) {
        printf("DSP-408 NOT found in event system services!\n");
        printf("First 5 services:\n");
        for (CFIndex i = 0; i < 5 && i < CFArrayGetCount(services); i++) {
            IOHIDServiceClientRef svc = (IOHIDServiceClientRef)CFArrayGetValueAtIndex(services, i);
            CFTypeRef regID = IOHIDServiceClientGetRegistryID(svc);
            CFTypeRef vid = IOHIDServiceClientCopyProperty(svc, CFSTR("VendorID"));
            CFTypeRef prod = IOHIDServiceClientCopyProperty(svc, CFSTR("Product"));
            printf("  [%ld] rid=", (long)i); printVal(regID);
            printf(" vid="); printVal(vid);
            printf(" prod="); printVal(prod); printf("\n");
            if (vid) CFRelease(vid);
            if (prod) CFRelease(prod);
        }
        CFRelease(services);
        CFRelease(client);
        return 1;
    }

    printf("\n=== Setting HIDDefaultBehavior=Yes via IOHIDServiceClientSetProperty ===\n");
    Boolean setResult = IOHIDServiceClientSetProperty(dspService, CFSTR("HIDDefaultBehavior"), CFSTR("Yes"));
    printf("SetProperty HIDDefaultBehavior: %s\n", setResult ? "true (success)" : "false (failed)");

    printf("\n=== Re-reading HIDDefaultBehavior from service ===\n");
    CFTypeRef val2 = IOHIDServiceClientCopyProperty(dspService, CFSTR("HIDDefaultBehavior"));
    printf("HIDDefaultBehavior: "); printVal(val2); printf("\n");
    if (val2) CFRelease(val2);

    printf("\n=== Checking IOHIDInterface in IORegistry after set ===\n");
    // (Use shell-out or direct IOKit to check IOHIDInterface.HIDDefaultBehavior)
    // Check via IOKit
    CFMutableDictionaryRef match = IOServiceMatching("IOHIDDevice");
    CFNumberRef vnum = CFNumberCreate(NULL, kCFNumberIntType, &(int){1155});
    CFNumberRef pnum = CFNumberCreate(NULL, kCFNumberIntType, &(int){22352});
    CFDictionarySetValue(match, CFSTR("VendorID"), vnum);
    CFDictionarySetValue(match, CFSTR("ProductID"), pnum);
    CFRelease(vnum); CFRelease(pnum);
    io_service_t hidDev = IOServiceGetMatchingService(kIOMainPortDefault, match);
    if (hidDev) {
        io_iterator_t iter = IO_OBJECT_NULL;
        IORegistryEntryGetChildIterator(hidDev, kIOServicePlane, &iter);
        io_service_t child;
        while ((child = IOIteratorNext(iter)) != IO_OBJECT_NULL) {
            char cn[256]; IOObjectGetClass(child, cn);
            if (strcmp(cn, "IOHIDInterface") == 0) {
                CFTypeRef v = IORegistryEntryCreateCFProperty(child, CFSTR("HIDDefaultBehavior"), kCFAllocatorDefault, 0);
                printf("IOHIDInterface.HIDDefaultBehavior (IORegistry): "); printVal(v); printf("\n");
                if (v) CFRelease(v);

                CFTypeRef dOES = IORegistryEntryCreateCFProperty(child, CFSTR("DeviceOpenedByEventSystem"), kCFAllocatorDefault, 0);
                printf("IOHIDInterface.DeviceOpenedByEventSystem: "); printVal(dOES); printf("\n");
                if (dOES) CFRelease(dOES);

                CFMutableDictionaryRef dp = NULL;
                IORegistryEntryCreateCFProperties(child, &dp, kCFAllocatorDefault, 0);
                if (dp) {
                    CFTypeRef ds = CFDictionaryGetValue(dp, CFSTR("DebugState"));
                    printf("IOHIDInterface.DebugState: "); printVal(ds); printf("\n");
                    CFRelease(dp);
                }
                IOObjectRelease(child);
                break;
            }
            IOObjectRelease(child);
        }
        IOObjectRelease(iter);
        IOObjectRelease(hidDev);
    }

    CFRelease(dspService);
    CFRelease(services);
    CFRelease(client);
    printf("\nDone.\n");
    return 0;
}
