#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <IOKit/usb/USBSpec.h>
#include <CoreFoundation/CoreFoundation.h>

#define VID 0x0483
#define PID 0x5750

static IOUSBInterfaceInterface300 **intf = NULL;

int main(void) {
    kern_return_t kr;
    io_service_t usbInterface;
    io_iterator_t iterator;
    
    // Match the USB interface (bInterfaceClass=3 HID)
    CFMutableDictionaryRef matchDict = IOServiceMatching("IOUSBInterface");
    if (!matchDict) { printf("IOServiceMatching failed\n"); return 1; }
    
    kr = IOServiceGetMatchingServices(kIOMainPortDefault, matchDict, &iterator);
    printf("IOServiceGetMatchingServices: %d\n", (int)kr);
    
    usbInterface = IO_OBJECT_NULL;
    io_service_t candidate;
    while ((candidate = IOIteratorNext(iterator)) != IO_OBJECT_NULL) {
        CFNumberRef vid_ref = (CFNumberRef)IORegistryEntryCreateCFProperty(
            candidate, CFSTR(kUSBVendorID), kCFAllocatorDefault, 0);
        CFNumberRef pid_ref = (CFNumberRef)IORegistryEntryCreateCFProperty(
            candidate, CFSTR(kUSBProductID), kCFAllocatorDefault, 0);
        
        if (vid_ref && pid_ref) {
            int vid = 0, pid = 0;
            CFNumberGetValue(vid_ref, kCFNumberIntType, &vid);
            CFNumberGetValue(pid_ref, kCFNumberIntType, &pid);
            CFRelease(vid_ref); CFRelease(pid_ref);
            
            if (vid == VID && pid == PID) {
                char name[128] = {0};
                IORegistryEntryGetName(candidate, name);
                printf("Found USB Interface: %s\n", name);
                usbInterface = candidate;
                break;
            }
        }
        if (vid_ref) CFRelease(vid_ref);
        if (pid_ref) CFRelease(pid_ref);
        IOObjectRelease(candidate);
    }
    IOObjectRelease(iterator);
    
    if (usbInterface == IO_OBJECT_NULL) {
        printf("USB Interface not found\n");
        return 1;
    }
    
    // Create plugin
    IOCFPlugInInterface **plugIn = NULL;
    SInt32 score = 0;
    kr = IOCreatePlugInInterfaceForService(
        usbInterface,
        kIOUSBInterfaceUserClientTypeID,
        kIOCFPlugInInterfaceID,
        &plugIn, &score);
    IOObjectRelease(usbInterface);
    printf("IOCreatePlugInInterfaceForService: %d\n", (int)kr);
    if (kr != kIOReturnSuccess || !plugIn) { printf("Plugin creation failed\n"); return 1; }
    
    // Get interface
    HRESULT hr = (*plugIn)->QueryInterface(plugIn, 
        CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID300),
        (LPVOID *)&intf);
    (*plugIn)->Release(plugIn);
    printf("QueryInterface: %08x\n", (unsigned)hr);
    if (hr != S_OK || !intf) { printf("Interface query failed\n"); return 1; }
    
    // Open (non-exclusive for the interface)
    kr = (*intf)->USBInterfaceOpen(intf);
    printf("USBInterfaceOpen: %08x\n", (unsigned)kr);
    if (kr != kIOReturnSuccess) {
        printf("Open failed - is DriverKit driver still attached?\n");
        (*intf)->Release(intf);
        return 1;
    }
    
    // Get endpoint count
    UInt8 numEP = 0;
    (*intf)->GetNumEndpoints(intf, &numEP);
    printf("NumEndpoints: %d\n", numEP);
    
    // Find interrupt endpoints
    for (UInt8 ep = 1; ep <= numEP; ep++) {
        UInt8 dir, num, type, interval;
        UInt16 maxPkt;
        (*intf)->GetEndpointProperties(intf, ep, &dir, &num, &type, &maxPkt, &interval);
        printf("  EP%d: dir=%d num=%d type=%d maxPkt=%d interval=%d\n",
               ep, dir, num, type, maxPkt, interval);
    }
    
    // Build frame: OP_POLL
    UInt8 frame[64] = {0x10, 0x02, 0x00, 0x01, 0x01, 0x40, 0x10, 0x03, 0x41};
    
    // Write to EP1 (out, pipe 1 typically)
    UInt32 wLen = 64;
    kr = (*intf)->WritePipe(intf, 1, frame, wLen);
    printf("WritePipe(1): %08x (len=%d)\n", (unsigned)kr, (int)wLen);
    
    // Read from EP2 (in, pipe 2 typically) 
    UInt8 buf[64] = {0};
    UInt32 rLen = 64;
    printf("ReadPipeTO(2, 2000ms)...\n");
    kr = (*intf)->ReadPipeTO(intf, 2, buf, &rLen, 2000, 2000);
    printf("ReadPipeTO: %08x (len=%d)\n", (unsigned)kr, (int)rLen);
    if (rLen > 0) {
        printf("  data: ");
        for (int i = 0; i < (int)rLen; i++) printf("%02x ", buf[i]);
        printf("\n");
    }
    
    (*intf)->USBInterfaceClose(intf);
    (*intf)->Release(intf);
    printf("Done\n");
    return 0;
}
