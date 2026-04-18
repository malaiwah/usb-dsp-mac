#!/usr/bin/env python3
"""analyze_capture.py — human-readable dump of a DSP-408 USB capture.

Uses tshark under the hood so you don't have to install scapy / pyshark.

What it prints:
  * A one-line summary (frame count, time span, endpoints seen)
  * Enumeration section (all control transfers through the first bulk/int
    transfer)
  * Bulk/Interrupt exchanges in time order, pairing OUT with the nearest
    following IN completion and printing decoded WMCU frame bytes.
  * Any "weird" frames (STALLs, zero-length completions where data was
    expected, etc.)

Usage:
    python3 analyze_capture.py captures/linux-02-vendor-probe.pcapng
    python3 analyze_capture.py captures/windows-01-fw-update-original-V6.21.pcapng

Requires tshark on the path (`brew install wireshark` on macOS).
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


FIELDS = [
    "frame.number",
    "frame.time_relative",
    "usb.endpoint_address",
    "usb.endpoint_address.direction",  # 1=IN, 0=OUT
    "usb.transfer_type",                # 0x00 iso, 0x01 int, 0x02 ctrl, 0x03 bulk
    "usb.urb_type",                     # 'S' submit, 'C' complete
    "usb.setup.bRequest",
    "usb.bmRequestType",
    "usb.urb_len",
    "usb.data_len",
    "usb.capdata",   # USBPcap (Windows) payloads
    "usbhid.data",   # Linux usbmon HID dissector payloads
]

TT = {"0x00": "ISO", "0x01": "INT", "0x02": "CTRL", "0x03": "BULK"}


def must_have_tshark() -> None:
    if not shutil.which("tshark"):
        sys.exit("tshark not found. `brew install wireshark` on macOS.")


def run_tshark(pcap: Path) -> list[dict]:
    cmd = [
        "tshark", "-r", str(pcap), "-T", "fields",
        "-E", "separator=|", "-E", "header=n", "-E", "quote=n",
    ]
    for f in FIELDS:
        cmd += ["-e", f]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    rows = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        parts = line.split("|")
        # pad missing
        while len(parts) < len(FIELDS):
            parts.append("")
        rows.append(dict(zip(FIELDS, parts)))
    return rows


def decode_dsp408_frame(hex_bytes: str) -> str | None:
    """Decode DSP-408 HID transport frame (magic 80 80 80 ee).

    Frame layout (64 bytes total):
      [0..3]  80 80 80 ee   magic
      [4]     a2 / 53       direction: a2=host→dev, 53=dev→host
      [5]     01            protocol version
      [6]     NN            sequence number
      [7]     09            constant
      [8..11] CMD[4]        command code (LE uint32)
      [12..13] LEN[2]       payload length (LE uint16)
      [14..]  DATA          payload (LEN bytes)
      [14+LEN] CHK          XOR of bytes[4..14+LEN-1]
      [15+LEN] aa           end marker
      rest: 00 padding
    """
    if not hex_bytes:
        return None
    hb = hex_bytes.replace(":", "").strip()
    try:
        b = bytes.fromhex(hb)
    except ValueError:
        return None
    if len(b) < 16 or b[0] != 0x80 or b[1] != 0x80 or b[2] != 0x80 or b[3] != 0xee:
        return None
    direction = "host→dev" if b[4] == 0xa2 else ("dev→host" if b[4] == 0x53 else f"dir={b[4]:02x}")
    seq = b[6]
    cmd = int.from_bytes(b[8:12], "little")
    data_len = int.from_bytes(b[12:14], "little")
    data = b[14:14 + data_len] if 14 + data_len <= len(b) else b""
    chk_pos = 14 + data_len
    if chk_pos >= len(b):
        return f"DSP408 dir={b[4]:02x} seq={seq} cmd=0x{cmd:02x} len={data_len} (frame too short)"
    chk = b[chk_pos]
    expected_chk = 0
    for i in range(4, min(chk_pos, len(b))):
        expected_chk ^= b[i]
    chk_ok = "✓" if chk == expected_chk else f"✗(got {chk:02x} expect {expected_chk:02x})"
    data_str = ""
    if data:
        printable = all(0x20 <= c < 0x7f for c in data if c != 0)
        stripped = data.rstrip(b"\x00")
        if printable and stripped:
            data_str = f'"{stripped.decode("ascii", errors="replace")}"'
        else:
            data_str = data.hex(" ")
    return (f"DSP408 {direction} seq={seq} cmd=0x{cmd:02x} len={data_len}"
            + (f" data={data_str}" if data_str else "")
            + f" chk={chk_ok}")


def decode_wmcu_frame(hex_bytes: str) -> str | None:
    """Return a one-line decode of a 64-byte vendor frame, or None if it
    doesn't look WMCU-framed.

    Vendor frame: 0x10 0x02 0x00 0x01 N CMD [payload..] 0x10 0x03 CHK  (pad 0x00)
    """
    if not hex_bytes:
        return None
    # hex string is a raw hex blob without separators from -e usb.capdata
    hb = hex_bytes.replace(":", "").strip()
    try:
        b = bytes.fromhex(hb)
    except ValueError:
        return None
    if len(b) < 9 or b[0] != 0x10 or b[1] != 0x02:
        return None
    n = b[4] if len(b) > 4 else 0
    cmd = b[5] if len(b) > 5 else 0
    # find DLE-ETX pair
    for i in range(6, min(len(b) - 2, 63)):
        if b[i] == 0x10 and b[i + 1] == 0x03:
            payload = b[6:i]
            chk = b[i + 2] if i + 2 < len(b) else 0
            xor = n ^ cmd
            for p in payload:
                xor ^= p
            good = "✓" if xor == chk else f"✗ (got {chk:02x}, expect {xor:02x})"
            pl = payload.hex(" ") if payload else "(none)"
            return (f"WMCU N={n} CMD=0x{cmd:02x} payload=[{pl}] "
                    f"chk=0x{chk:02x} {good}")
    return "WMCU (no DLE-ETX found)"


def classify(row: dict) -> str:
    tt = TT.get(row.get("usb.transfer_type", ""), row.get("usb.transfer_type", "?"))
    urb = row.get("usb.urb_type", "?")
    ep = row.get("usb.endpoint_address", "?")
    d = row.get("usb.endpoint_address.direction", "?")
    d_str = "IN" if d == "1" else ("OUT" if d == "0" else "?")
    return f"{tt} {urb} ep={ep} {d_str}"


def decode_setup(row: dict) -> str:
    req = row.get("usb.setup.bRequest", "")
    rt = row.get("usb.bmRequestType", "")
    return f"bRequest={req} bmReqType={rt}"


def analyze(pcap: Path) -> None:
    print(f"=== {pcap} ===")
    rows = run_tshark(pcap)
    print(f"frames: {len(rows)}")
    if not rows:
        return

    t_first = rows[0].get("frame.time_relative", "0")
    t_last  = rows[-1].get("frame.time_relative", "0")
    print(f"time span: {t_first}  →  {t_last}s")

    endpoints = {}
    for r in rows:
        ep = r.get("usb.endpoint_address", "")
        endpoints[ep] = endpoints.get(ep, 0) + 1
    print(f"endpoints seen: {dict(sorted(endpoints.items()))}")

    # Split enumeration (all control) from later (bulk/int)
    enum_end = 0
    for i, r in enumerate(rows):
        if r.get("usb.transfer_type") not in ("0x02", "", None):
            enum_end = i
            break
    else:
        enum_end = len(rows)

    print()
    print(f"-- Enumeration / control transfers (frames 1..{enum_end}) --")
    for r in rows[:enum_end]:
        fn = r["frame.number"]
        t  = r["frame.time_relative"]
        raw_hex = r.get("usbhid.data", "") or r.get("usb.capdata", "")
        hexd = decode_dsp408_frame(raw_hex) or decode_wmcu_frame(raw_hex)
        extra = f"   PROTO: {hexd}" if hexd else ""
        print(f"  #{fn:>4s} t={t:>12s}  {classify(r)}  {decode_setup(r)}"
              f" ulen={r.get('usb.urb_len','')} dlen={r.get('usb.data_len','')}"
              f"{extra}")

    print()
    print(f"-- Interrupt / Bulk traffic (frames {enum_end+1}..{len(rows)}) --")
    prev_cmd = None
    for r in rows[enum_end:]:
        fn = r["frame.number"]
        t  = r["frame.time_relative"]
        raw_hex = r.get("usbhid.data", "") or r.get("usb.capdata", "")
        hexd = decode_dsp408_frame(raw_hex) or decode_wmcu_frame(raw_hex)
        extra = f"   PROTO: {hexd}" if hexd else ""
        is_in  = (r.get("usb.endpoint_address.direction") == "1")
        tag = "  ← IN " if is_in else "  → OUT"
        dlen = r.get("usb.data_len", "")
        # Only note IN completions with data_len > 0 — those are device replies
        note = ""
        urb_type = r.get("usb.urb_type", "")
        # USBPcap (Windows) leaves urb_type empty; Linux usbmon uses "C"/"S"
        is_completion = urb_type in ("C", "")
        if is_completion and is_in and dlen and dlen != "0":
            note = f"  ** {dlen} bytes from device **"
        print(f"  #{fn:>4s} t={t:>12s} {tag} {classify(r)} "
              f"ulen={r.get('usb.urb_len','')} dlen={dlen}{note}{extra}")

    # Summary: did any IN-direction frame carry data?
    # Useful summary — did the device EVER send interrupt-IN data?
    in_int_data = [r for r in rows
                   if r.get("usb.transfer_type") == "0x01"
                   and r.get("usb.endpoint_address.direction") == "1"
                   and r.get("usb.urb_type", "") in ("C", "")  # "C"=Linux, ""=USBPcap
                   and r.get("usb.data_len", "0") not in ("", "0")]
    print()
    print(f"Interrupt-IN completions with data (= device→host replies): "
          f"{len(in_int_data)}")
    for r in in_int_data[:20]:
        fn = r["frame.number"]
        t  = r["frame.time_relative"]
        hd = r.get("usbhid.data", "") or r.get("usb.capdata", "")
        print(f"  #{fn:>4s} t={t}  ep={r.get('usb.endpoint_address','')} "
              f"dlen={r.get('usb.data_len','')}  hex={hd[:160]}")


def main() -> None:
    must_have_tshark()
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap", nargs="+", type=Path)
    args = ap.parse_args()
    for p in args.pcap:
        if not p.exists():
            print(f"(skip, not found) {p}")
            continue
        analyze(p)
        print()


if __name__ == "__main__":
    main()
