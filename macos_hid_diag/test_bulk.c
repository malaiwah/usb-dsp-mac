#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libusb-1.0/libusb.h>

#define VID 0x0483
#define PID 0x5750
#define EP_OUT 0x01
#define EP_IN  0x82

int main() {
    libusb_context *ctx = NULL;
    libusb_device_handle *handle = NULL;
    
    int r = libusb_init(&ctx);
    printf("libusb_init: %d\n", r);
    
    handle = libusb_open_device_with_vid_pid(ctx, VID, PID);
    if (!handle) { printf("Device not found\n"); return 1; }
    printf("Device opened\n");
    
    r = libusb_kernel_driver_active(handle, 0);
    printf("kernel_driver_active: %d\n", r);
    if (r) {
        r = libusb_detach_kernel_driver(handle, 0);
        printf("detach_kernel_driver: %d\n", r);
    }
    
    r = libusb_claim_interface(handle, 0);
    printf("claim_interface: %d\n", r);
    
    // Build OP_POLL frame
    unsigned char frame[64] = {0x10, 0x02, 0x00, 0x01, 0x01, 0x40, 0x10, 0x03, 0x41};
    
    // Try interrupt write
    int transferred = 0;
    r = libusb_interrupt_transfer(handle, EP_OUT, frame, 64, &transferred, 1000);
    printf("interrupt_write: r=%d transferred=%d\n", r, transferred);
    
    // Try interrupt read
    unsigned char buf[64] = {0};
    printf("interrupt_read (2s)...\n");
    r = libusb_interrupt_transfer(handle, EP_IN, buf, 64, &transferred, 2000);
    printf("interrupt_read: r=%d transferred=%d\n", r, transferred);
    if (transferred > 0) {
        printf("data: ");
        for (int i=0; i<transferred; i++) printf("%02x ", buf[i]);
        printf("\n");
    }
    
    // Try as bulk transfer (treating interrupt endpoint as bulk)
    printf("Sending again, then bulk_read (2s)...\n");
    libusb_interrupt_transfer(handle, EP_OUT, frame, 64, &transferred, 1000);
    r = libusb_bulk_transfer(handle, EP_IN, buf, 64, &transferred, 2000);
    printf("bulk_read: r=%d transferred=%d\n", r, transferred);
    if (transferred > 0) {
        printf("data: ");
        for (int i=0; i<transferred; i++) printf("%02x ", buf[i]);
        printf("\n");
    }
    
    libusb_release_interface(handle, 0);
    libusb_attach_kernel_driver(handle, 0);
    libusb_close(handle);
    libusb_exit(ctx);
    return 0;
}
