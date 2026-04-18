#!/usr/bin/env python3
"""probe.py — Linux-only. Drives the DSP-408 with a short list of vendor
HID frames and prints any input reports that come back.

This is the experiment we've never been able to run on macOS because input
reports get filtered before reaching userspace. On Linux the kernel hands
input reports straight to /dev/hidrawN, so we can finally see the device's
side of the conversation.

Run with Wireshark capturing on the appropriate usbmonN interface; save the
capture as captures/linux-02-vendor-probe.pcapng.

Requires:
    sudo apt install libhidapi-libusb0
    pip install --user hidapi

Run as root (or after `sudo chmod 666 /dev/hidrawN`):
    sudo python3 probe.py
"""
from __future__ import annotations
import sys
import time

try:
    import hid  # PyPI `hidapi` (new API) OR Debian `python3-hid` (legacy API)
except ImportError:
    sys.exit("Need the hidapi Python package: pip install --user hidapi  "
             "(or `apt install python3-hid` on Debian/Pi)")

VID = 0x0483
PID = 0x5750


# ---- API compatibility shim ----------------------------------------------
# Two flavours of the `hid` module exist in the wild:
#   * Newer `hidapi` PyPI package: `h = hid.Device(VID, PID); h.write(b); h.read(n, t)`
#   * Older `hid` PyPI package / Debian `python3-hid`:
#         `h = hid.device(); h.open(VID, PID); h.write(b); h.read(n, t)`
# The methods accept the same args; we just need different open/close.
def _open_device(vid: int, pid: int):
    if hasattr(hid, "Device"):
        return hid.Device(vid, pid)              # new API
    h = hid.device()                             # old API
    h.open(vid, pid)
    return h


def _set_nonblocking(h, value: bool) -> None:
    if hasattr(h, "nonblocking"):
        try:
            h.nonblocking = value                # property on new API
            return
        except AttributeError:
            pass
    if hasattr(h, "set_nonblocking"):
        h.set_nonblocking(1 if value else 0)    # method on old API


def _device_strings(h) -> str:
    def call(name):
        v = getattr(h, name, None)
        try:
            return v() if callable(v) else v
        except Exception:
            return None
    parts = []
    for label, attr in [("manufacturer", "manufacturer"),
                        ("manufacturer", "get_manufacturer_string"),
                        ("product",      "product"),
                        ("product",      "get_product_string"),
                        ("serial",       "serial"),
                        ("serial",       "get_serial_number_string")]:
        v = call(attr)
        if v:
            parts.append((label, v))
    # Dedup while keeping first occurrence per label
    seen = set(); out = []
    for k, v in parts:
        if k in seen: continue
        seen.add(k); out.append(f"  {k:12s} = {v!r}")
    return "\n".join(out)

# Command bytes to probe. The framing comes from analyze_wmcu.py and the
# corehid_*.swift experiments — `0x10 0x02 0x00 0x01 N CMD 0x10 0x03 CHK`
# wrapped in a 64-byte HID Output report (report ID 0).
#
# Add bytes here to test more commands. Common DSP "get" commands that we'd
# expect to be cheap and side-effect-free:
#   0x05 — current preset / firmware-version-ish
#   0x01..0x04 — generic gets
#   0x06..0x0F — try a few unknowns
CMDS: list[int] = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09,
                   0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x20, 0x40,
                   0x80, 0xA0]

DELAY_BETWEEN_CMDS_S = 0.4
READ_TIMEOUT_MS      = 250


def make_frame(cmd: int) -> bytes:
    """Vendor frame: 0x10 0x02 0x00 0x01 N CMD 0x10 0x03 CHK + zero-pad to 64."""
    n = 1
    chk = n ^ cmd
    body = bytes([0x10, 0x02, 0x00, 0x01, n, cmd, 0x10, 0x03, chk])
    return body + bytes(64 - len(body))


def hex16(b: bytes, n: int = 16) -> str:
    return " ".join(f"{x:02x}" for x in b[:n])


def main() -> None:
    print(f"Opening HID device VID=0x{VID:04x} PID=0x{PID:04x} ...")
    try:
        h = _open_device(VID, PID)
    except Exception as e:
        sys.exit(f"open failed: {e}")

    info = _device_strings(h)
    if info:
        print(info)

    _set_nonblocking(h, False)  # blocking reads — we use the explicit timeout

    # Drain any pending input reports first
    print("\nDraining stale input reports ...")
    drained = 0
    while True:
        r = h.read(64, 50)  # positional (legacy hid.device.read() rejects keyword)
        if not r:
            break
        drained += 1
        print(f"  drained: {hex16(bytes(r))}")
    print(f"  done ({drained} drained)\n")

    print(f"Probing {len(CMDS)} commands ({DELAY_BETWEEN_CMDS_S}s spacing)\n")
    for cmd in CMDS:
        frame = make_frame(cmd)
        # report ID 0 = no report ID; hidapi prepends a 0 byte automatically
        # if the device's report descriptor declares no report ID.
        print(f"── cmd 0x{cmd:02x} ── tx: {hex16(frame, 16)} …")
        n = h.write(b"\x00" + frame)
        if n <= 0:
            print(f"   ✗ write failed (return {n})")
            continue

        # Drain replies for the configured timeout window
        start = time.monotonic()
        rx_count = 0
        while (time.monotonic() - start) * 1000 < READ_TIMEOUT_MS:
            r = h.read(64, READ_TIMEOUT_MS)  # positional for legacy API
            if not r:
                break
            rx_count += 1
            print(f"   rx#{rx_count}: {hex16(bytes(r), 32)}")
        if rx_count == 0:
            print("   (no input reports in window)")

        time.sleep(DELAY_BETWEEN_CMDS_S)

    h.close()
    print("\nDone. Save the Wireshark capture as captures/linux-02-vendor-probe.pcapng")


if __name__ == "__main__":
    main()
