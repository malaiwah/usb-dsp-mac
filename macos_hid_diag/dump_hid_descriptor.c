// dump_hid_descriptor.c — pulls the raw HID Report Descriptor for the DSP-408
// straight out of the IORegistry, then prints it as hex + a human-readable
// breakdown of the first few HID items so we can confirm the Usage Page.
//
// Compile:
//   clang -o dump_hid_descriptor dump_hid_descriptor.c \
//       -framework IOKit -framework CoreFoundation
//
// Run:
//   ./dump_hid_descriptor

#include <stdio.h>
#include <string.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/hid/IOHIDKeys.h>
#include <CoreFoundation/CoreFoundation.h>

#define VID 1155     // 0x0483
#define PID 22352    // 0x5750

static const char *usage_page_name(int page) {
    switch (page) {
        case 0x01: return "Generic Desktop";
        case 0x02: return "Simulation";
        case 0x03: return "VR";
        case 0x04: return "Sport";
        case 0x05: return "Game";
        case 0x06: return "Generic Device";
        case 0x07: return "Keyboard/Keypad";
        case 0x08: return "LEDs";
        case 0x09: return "Button";
        case 0x0A: return "Ordinal";
        case 0x0B: return "Telephony";
        case 0x0C: return "Consumer";
        case 0x0D: return "Digitizer";
        case 0x0F: return "Physical Interface";
        case 0x10: return "Unicode";
        case 0x14: return "Alphanumeric Display";
        case 0x40: return "Medical";
        case 0x80 ... 0x83: return "Monitor / Power";
        case 0x84 ... 0x87: return "Power Device";
        case 0x8C: return "Bar Code Scanner";
        case 0x8D: return "Scale";
        case 0x8E: return "Magnetic Stripe Reader";
        case 0x90: return "Camera Control";
        case 0x91: return "Arcade";
        default:
            if (page >= 0xFF00) return "*** VENDOR-DEFINED *** (root cause if hidd doesn't recognize it)";
            return "(reserved/unknown)";
    }
}

static void parse_descriptor(const uint8_t *d, size_t n) {
    printf("\n=== Parsed HID items (first ~20) ===\n");
    size_t i = 0;
    int items_shown = 0;
    int current_usage_page = -1;

    while (i < n && items_shown < 20) {
        uint8_t prefix = d[i];
        uint8_t size_code = prefix & 0x03;
        uint8_t type      = (prefix >> 2) & 0x03;  // 0=Main 1=Global 2=Local
        uint8_t tag       = (prefix >> 4) & 0x0F;
        size_t  data_len  = (size_code == 3) ? 4 : size_code;

        if (i + 1 + data_len > n) { printf("  [truncated at offset %zu]\n", i); break; }

        // Read little-endian data
        uint32_t data = 0;
        for (size_t k = 0; k < data_len; k++) data |= ((uint32_t)d[i+1+k]) << (8*k);

        const char *type_name = (type==0)?"Main":(type==1)?"Global":(type==2)?"Local":"Reserved";
        printf("  +%-4zu  %02x %s [type=%s tag=0x%X data=0x%X (%u)]",
               i, prefix, "  ", type_name, tag, data, data);

        // Decode notable items
        if (type == 1 && tag == 0) {
            current_usage_page = (int)data;
            printf("  ← Usage Page = 0x%X (%s)", data, usage_page_name(data));
            if (data >= 0xFF00) {
                printf("\n           >>> THIS IS THE PROBLEM <<<");
            }
        } else if (type == 2 && tag == 0) {
            printf("  ← Usage = 0x%X", data);
        } else if (type == 0 && tag == 0xA) {
            printf("  ← Collection (0x%X)", data);
        } else if (type == 0 && tag == 0xC) {
            printf("  ← End Collection");
        } else if (type == 0 && tag == 0x8) {
            printf("  ← Input (0x%X)", data);
        } else if (type == 0 && tag == 0x9) {
            printf("  ← Output (0x%X)", data);
        } else if (type == 0 && tag == 0xB) {
            printf("  ← Feature (0x%X)", data);
        } else if (type == 1 && tag == 7) {
            printf("  ← Report Size = %u bits", data);
        } else if (type == 1 && tag == 9) {
            printf("  ← Report Count = %u", data);
        } else if (type == 1 && tag == 8) {
            printf("  ← Report ID = 0x%X", data);
        }
        printf("\n");

        i += 1 + data_len;
        items_shown++;
    }

    printf("\n=== Diagnosis ===\n");
    if (current_usage_page >= 0xFF00) {
        printf("✓ Confirmed: Usage Page = 0x%X is VENDOR-DEFINED.\n", current_usage_page);
        printf("  This is exactly what AppleUserHIDDrivers.dext rejects with HIDDefaultBehavior=\"\".\n");
        printf("  Firmware patch target: change Usage Page bytes (06 %02X %02X) to 06 00 0C\n",
               current_usage_page & 0xFF, (current_usage_page >> 8) & 0xFF);
        printf("  …or 05 0C (Consumer Control, short form) if the firmware uses 06 instead of 05.\n");
    } else {
        printf("? Usage Page 0x%X is recognized — root cause may be different than assumed.\n",
               current_usage_page);
    }
}

int main(void) {
    printf("=== DSP-408 HID Report Descriptor dump ===\n");
    printf("    VID=0x%04X PID=0x%04X\n\n", VID, PID);

    CFMutableDictionaryRef match = IOServiceMatching("IOHIDDevice");
    CFNumberRef vid = CFNumberCreate(NULL, kCFNumberIntType, &(int){VID});
    CFNumberRef pid = CFNumberCreate(NULL, kCFNumberIntType, &(int){PID});
    CFDictionarySetValue(match, CFSTR(kIOHIDVendorIDKey), vid);
    CFDictionarySetValue(match, CFSTR(kIOHIDProductIDKey), pid);
    CFRelease(vid); CFRelease(pid);

    io_service_t dev = IOServiceGetMatchingService(kIOMainPortDefault, match);
    if (!dev) { fprintf(stderr, "Device not found.\n"); return 1; }

    CFTypeRef desc = IORegistryEntryCreateCFProperty(
        dev, CFSTR(kIOHIDReportDescriptorKey), kCFAllocatorDefault, 0);
    if (!desc) {
        fprintf(stderr, "ReportDescriptor property not present (key=%s).\n",
                kIOHIDReportDescriptorKey);
        IOObjectRelease(dev);
        return 2;
    }
    if (CFGetTypeID(desc) != CFDataGetTypeID()) {
        fprintf(stderr, "ReportDescriptor isn't CFData.\n");
        CFRelease(desc); IOObjectRelease(dev); return 3;
    }

    CFIndex n = CFDataGetLength(desc);
    const uint8_t *bytes = CFDataGetBytePtr(desc);
    printf("ReportDescriptor: %ld bytes\n\n", (long)n);

    // Hex dump
    printf("=== Raw bytes ===\n");
    for (CFIndex i = 0; i < n; i++) {
        if (i % 16 == 0) printf("  %04lx:  ", (long)i);
        printf("%02x ", bytes[i]);
        if (i % 16 == 15 || i == n-1) printf("\n");
    }

    // C-array form (handy for firmware grep)
    printf("\n=== C-array form (for binary searching the firmware) ===\n  ");
    for (CFIndex i = 0; i < n; i++) {
        printf("0x%02x,%s", bytes[i], (i % 12 == 11) ? "\n  " : " ");
    }
    printf("\n");

    parse_descriptor(bytes, (size_t)n);

    CFRelease(desc);
    IOObjectRelease(dev);
    return 0;
}
