#!/usr/bin/env python3
"""Scan DSP-408.exe for WMCU/firmware/crypto/version strings.

Goal: locate where the Windows app stores or generates the WMCU container,
and identify which crypto routines (CryptoAPI, OpenSSL, mbedTLS, ...) it
links so we can predict what algorithm produced the 8-byte trailer signature
in DSP-408-Firmware.bin.
"""
from pathlib import Path

EXE = Path("/Users/mbelleau/Code/usb_dsp_mac/downloads/DSP-408-Windows/"
           "DSP-408-Windows-V1.24 190622/DSP-408.exe")

NEEDLES = [
    (b"WMCU",            "WMCU magic"),
    (b"MYDW-AV",         "version string"),
    (b"/ZZZ",            "WMCU trailer"),
    (b"DSP-408-Firmware","embedded fw filename"),
    (b"DSP-408",         "DSP-408 literal"),
    (b".bin",            ".bin extension"),
    (b"STM32",           "STM32"),
    (b"BootLoader",      "BootLoader (CC)"),
    (b"bootloader",      "bootloader (lc)"),
    (b"Bootloader",      "Bootloader (Mc)"),
    (b"firmware",        "firmware (lc)"),
    (b"Firmware",        "Firmware (Mc)"),
    (b"CRC",             "CRC literal"),
    (b"crc32",           "crc32 literal"),
    (b"CRC32",           "CRC32 literal"),
    (b"MD5",             "MD5 literal"),
    (b"sha1",            "sha1 literal"),
    (b"SHA1",            "SHA1 literal"),
    (b"sha256",          "sha256 literal"),
    (b"SHA256",          "SHA256 literal"),
    (b"AES",             "AES literal"),
    (b"RSA",             "RSA literal"),
    (b"DES",             "DES literal"),
    (b"HMAC",            "HMAC literal"),
    (b"mbedtls",         "mbedTLS"),
    (b"OpenSSL",         "OpenSSL"),
    (b"libtomcrypt",     "libtomcrypt"),
    (b"BCryptHashData",  "CryptoAPI BCrypt"),
    (b"CryptHashData",   "CryptoAPI legacy"),
    (b"CryptCreateHash", "CryptoAPI legacy"),
    (b"WriteFile",       "WriteFile (kernel32)"),
    (b"ReadFile",        "ReadFile (kernel32)"),
    (b"HidD_SetOutputReport","HID API setoutput"),
    (b"HidD_SetFeature", "HID API setfeature"),
    (b"HidD_GetFeature", "HID API getfeature"),
    (b"HidP_",           "HID parser"),
]


def find_all(data: bytes, needle: bytes) -> list[int]:
    out, pos = [], 0
    while True:
        i = data.find(needle, pos)
        if i < 0:
            return out
        out.append(i)
        pos = i + 1


def main() -> None:
    data = EXE.read_bytes()
    print(f"File: {EXE.name}")
    print(f"Size: {len(data):,} bytes ({len(data) / 1024 / 1024:.1f} MB)\n")
    for needle, label in NEEDLES:
        hits = find_all(data, needle)
        if not hits:
            continue
        head = ", ".join(f"0x{h:x}" for h in hits[:6])
        more = f" (+{len(hits) - 6} more)" if len(hits) > 6 else ""
        print(f"{label:25s} {len(hits):5d}× : {head}{more}")


if __name__ == "__main__":
    main()
