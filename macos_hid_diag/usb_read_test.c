/*
 * DSP-408 IOUSBLib interrupt pipe test.
 * Uses ReadPipeAsync (correct for interrupt pipes) + CFRunLoop.
 *
 * Build: clang -o /tmp/usb_read_test /tmp/usb_read_test.c \
 *        -framework IOKit -framework CoreFoundation -Wall
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <CoreFoundation/CoreFoundation.h>
#include <mach/mach_time.h>

#define VID         0x0483
#define PID         0x5750
#define REPORT_SIZE 64

typedef IOUSBInterfaceInterface183 USBIntf;

static volatile int g_got_data = 0;
static uint8_t g_recv_buf[REPORT_SIZE];
static UInt32  g_recv_len = 0;

static void build_frame(uint8_t cmd, uint8_t *out) {
    memset(out, 0, REPORT_SIZE);
    out[0]=0x10; out[1]=0x02; out[2]=0x00; out[3]=0x01;
    out[4]=0x01;  /* length=1 */
    out[5]=cmd;
    out[6]=0x10; out[7]=0x03;
    out[8]=0x01^cmd;
}

/* Async read callback */
static void read_cb(void *refcon, IOReturn res, void *arg0) {
    UInt32 len = (UInt32)(uintptr_t)arg0;
    printf("  [CALLBACK] IOReturn=%08x len=%u\n", res, len);
    if (res == kIOReturnSuccess && len > 0) {
        g_recv_len = len;
        memcpy(g_recv_buf, (uint8_t*)refcon, len < REPORT_SIZE ? len : REPORT_SIZE);
        g_got_data = 1;
    }
    /* Stop the run loop */
    CFRunLoopStop(CFRunLoopGetCurrent());
}

int main(void) {
    kern_return_t kr;
    io_iterator_t dev_iter = IO_OBJECT_NULL;
    io_service_t device_svc = IO_OBJECT_NULL;
    io_service_t iface_svc  = IO_OBJECT_NULL;
    IOCFPlugInInterface **dev_plug  = NULL;
    IOCFPlugInInterface **intf_plug = NULL;
    IOUSBDeviceInterface **dev_intf = NULL;
    USBIntf **intf = NULL;
    SInt32 score = 0;

    printf("=== DSP-408 IOUSBLib Interrupt Async Read Test ===\n\n");

    /* 1. Find USB device */
    CFMutableDictionaryRef match = IOServiceMatching(kIOUSBDeviceClassName);
    SInt32 vid = VID, pid = PID;
    CFDictionarySetValue(match, CFSTR(kUSBVendorID),
        CFNumberCreate(kCFAllocatorDefault, kCFNumberSInt32Type, &vid));
    CFDictionarySetValue(match, CFSTR(kUSBProductID),
        CFNumberCreate(kCFAllocatorDefault, kCFNumberSInt32Type, &pid));

    kr = IOServiceGetMatchingServices(kIOMainPortDefault, match, &dev_iter);
    device_svc = IOIteratorNext(dev_iter);
    IOObjectRelease(dev_iter);
    if (device_svc == IO_OBJECT_NULL) {
        fprintf(stderr, "Device not found\n"); return 1;
    }
    printf("[1] Found device %u\n", device_svc);

    /* 2. Create device interface */
    kr = IOCreatePlugInInterfaceForService(device_svc,
         kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID,
         &dev_plug, &score);
    IOObjectRelease(device_svc);
    if (kr || !dev_plug) { fprintf(stderr, "device plugin failed\n"); return 1; }

    (*dev_plug)->QueryInterface(dev_plug,
         CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID), (LPVOID*)&dev_intf);
    (*dev_plug)->Release(dev_plug);
    if (!dev_intf) { fprintf(stderr, "device QI failed\n"); return 1; }
    printf("[2] Got device interface\n");

    /* 3. Open device */
    kr = (*dev_intf)->USBDeviceOpen(dev_intf);
    printf("[3] USBDeviceOpen: 0x%08x\n", kr);

    /* 4. Get interface iterator */
    io_iterator_t intf_iter = IO_OBJECT_NULL;
    IOUSBFindInterfaceRequest req = {
        kIOUSBFindInterfaceDontCare, kIOUSBFindInterfaceDontCare,
        kIOUSBFindInterfaceDontCare, kIOUSBFindInterfaceDontCare
    };
    (*dev_intf)->CreateInterfaceIterator(dev_intf, &req, &intf_iter);
    iface_svc = IOIteratorNext(intf_iter);
    IOObjectRelease(intf_iter);
    if (!iface_svc) { fprintf(stderr, "no interface\n"); goto out_dev; }
    printf("[4] Found interface %u\n", iface_svc);

    /* 5. Create interface plugin (ID183 = has OpenSeize + ReadPipeAsync) */
    kr = IOCreatePlugInInterfaceForService(iface_svc,
         kIOUSBInterfaceUserClientTypeID, kIOCFPlugInInterfaceID,
         &intf_plug, &score);
    IOObjectRelease(iface_svc);
    if (kr || !intf_plug) { fprintf(stderr, "intf plugin failed\n"); goto out_dev; }

    (*intf_plug)->QueryInterface(intf_plug,
         CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID183), (LPVOID*)&intf);
    (*intf_plug)->Release(intf_plug);
    if (!intf) { fprintf(stderr, "intf QI failed\n"); goto out_dev; }
    printf("[5] Got IOUSBInterfaceInterface183\n");

    /* 6. Create async event source on main run loop */
    mach_port_t async_port = MACH_PORT_NULL;
    kr = (*intf)->CreateInterfaceAsyncPort(intf, &async_port);
    printf("[6a] CreateInterfaceAsyncPort: 0x%08x port=%u\n", kr, async_port);

    CFRunLoopSourceRef rl_src = NULL;
    kr = (*intf)->CreateInterfaceAsyncEventSource(intf, &rl_src);
    printf("[6b] CreateInterfaceAsyncEventSource: 0x%08x src=%p\n", kr, (void*)rl_src);
    if (rl_src) {
        CFRunLoopAddSource(CFRunLoopGetCurrent(), rl_src, kCFRunLoopDefaultMode);
        CFRelease(rl_src);
        printf("[6c] Added run loop source to current run loop\n");
    }

    /* 7. SEIZE interface */
    kr = (*intf)->USBInterfaceOpenSeize(intf);
    printf("[7] USBInterfaceOpenSeize: 0x%08x %s\n", kr, kr == 0 ? "OK" : "FAILED");
    if (kr) {
        kr = (*intf)->USBInterfaceOpen(intf);
        printf("[7b] USBInterfaceOpen: 0x%08x %s\n", kr, kr == 0 ? "OK" : "FAILED");
        if (kr) goto out_intf;
    }

    /* 8. Enumerate pipes */
    uint8_t num_eps = 0;
    (*intf)->GetNumEndpoints(intf, &num_eps);
    printf("[8] NumEndpoints: %d\n", num_eps);

    uint8_t in_pipe = 0, out_pipe = 0;
    for (uint8_t p = 1; p <= num_eps; p++) {
        uint8_t dir, num, ttype, interval;
        uint16_t maxpkt;
        (*intf)->GetPipeProperties(intf, p, &dir, &num, &ttype, &maxpkt, &interval);
        uint8_t addr = (dir == kUSBIn) ? (0x80|num) : num;
        printf("    Pipe %d: %s addr=0x%02x type=%d maxPkt=%d interval=%d\n",
               p, dir==kUSBIn?"IN ":"OUT", addr, ttype, maxpkt, interval);
        if (dir == kUSBIn  && addr == 0x82) in_pipe  = p;
        if (dir == kUSBOut && addr == 0x01) out_pipe = p;
    }
    printf("    -> in_pipe=%d out_pipe=%d\n", in_pipe, out_pipe);
    if (!in_pipe || !out_pipe) { fprintf(stderr, "pipes not found\n"); goto out_close; }

    /* 9. Clear stall */
    (*intf)->ClearPipeStall(intf, in_pipe);
    (*intf)->ClearPipeStall(intf, out_pipe);
    printf("[9] ClearPipeStall on both pipes\n");

    /* 10. Write+Read cycles using ReadPipeAsync (correct for interrupt) */
    static const uint8_t cmds[] = {0x10, 0x40, 0x13, 0x10, 0x40};
    static const char *names[] = {"OP_INIT", "OP_POLL", "OP_FW", "OP_INIT(2)", "OP_POLL(2)"};

    for (int c = 0; c < 5; c++) {
        printf("\n[10.%d] %s\n", c, names[c]);

        /* Submit async read BEFORE write */
        g_got_data = 0;
        static uint8_t recv_storage[REPORT_SIZE];
        memset(recv_storage, 0, REPORT_SIZE);

        kr = (*intf)->ReadPipeAsync(intf, in_pipe,
                                    recv_storage, REPORT_SIZE,
                                    read_cb, recv_storage);
        printf("      ReadPipeAsync: 0x%08x\n", kr);
        if (kr) {
            printf("      -> ReadPipeAsync failed, skipping\n");
            continue;
        }

        /* Write the command */
        uint8_t frame[REPORT_SIZE];
        build_frame(cmds[c], frame);
        UInt32 wlen = REPORT_SIZE;
        kr = (*intf)->WritePipeTO(intf, out_pipe, frame, wlen, 1000, 1000);
        printf("      WritePipeTO: 0x%08x\n", kr);

        /* Pump the run loop for up to 2 seconds */
        printf("      Pumping run loop (2s)...\n");
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 2.0, false);

        if (g_got_data) {
            printf("      GOT DATA (%u bytes): ", g_recv_len);
            for (UInt32 i = 0; i < g_recv_len && i < 16; i++) {
                printf("%02x ", g_recv_buf[i]);
            }
            printf("\n");
        } else {
            printf("      No data received\n");
            /* Abort pending read */
            (*intf)->AbortPipe(intf, in_pipe);
        }
    }

    /* 11. Read-only test (no write) */
    printf("\n[11] Read-only: submit async read, wait 3s, no write\n");
    g_got_data = 0;
    static uint8_t recv2[REPORT_SIZE];
    kr = (*intf)->ReadPipeAsync(intf, in_pipe, recv2, REPORT_SIZE, read_cb, recv2);
    printf("     ReadPipeAsync: 0x%08x\n", kr);
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 3.0, false);
    if (g_got_data) {
        printf("     GOT UNSOLICITED DATA\n");
    } else {
        printf("     No data (device silent without commands)\n");
        (*intf)->AbortPipe(intf, in_pipe);
    }

out_close:
    (*intf)->USBInterfaceClose(intf);
out_intf:
    (*intf)->Release(intf);
out_dev:
    (*dev_intf)->USBDeviceClose(dev_intf);
    (*dev_intf)->Release(dev_intf);
    printf("\n[Done]\n");
    return 0;
}
