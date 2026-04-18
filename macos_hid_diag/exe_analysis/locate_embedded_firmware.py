#!/usr/bin/env python3
"""Locate (and carve, if present) the firmware blob embedded inside DSP-408.exe.

Strategy:
1. Find offset of WMCU magic in both .exe and DSP-408-Firmware.bin.
2. Verify the .exe and the .bin share an aligned identical region around the
   WMCU header — if so, the .exe really embeds the firmware verbatim.
3. Carve the embedded blob to disk for offline diff.
4. Print key offsets the user can hand to radare2 / objdump for further work.
"""
from pathlib import Path

ROOT = Path("/Users/mbelleau/Code/usb_dsp_mac")
EXE = ROOT / "downloads/DSP-408-Windows/DSP-408-Windows-V1.24 190622/DSP-408.exe"
FW = ROOT / "downloads/DSP-408-Firmware.bin"
OUT_DIR = Path(__file__).resolve().parent
OUT_FW = OUT_DIR / "embedded_firmware.bin"


def find_one(data: bytes, needle: bytes) -> int:
    i = data.find(needle)
    if i < 0:
        raise SystemExit(f"needle {needle!r} not found")
    if data.find(needle, i + 1) >= 0:
        # not necessarily fatal, but warn
        print(f"  warn: {needle!r} appears more than once in this file")
    return i


def main() -> None:
    exe = EXE.read_bytes()
    fw = FW.read_bytes()
    print(f".exe size: {len(exe):,}    .bin size: {len(fw):,}\n")

    exe_wmcu = find_one(exe, b"WMCU")
    fw_wmcu = find_one(fw, b"WMCU")
    print(f"WMCU magic — .exe @ 0x{exe_wmcu:08x}, .bin @ 0x{fw_wmcu:08x}")

    exe_ver = find_one(exe, b"MYDW-AV")
    fw_ver = find_one(fw, b"MYDW-AV")
    print(f"MYDW-AV    — .exe @ 0x{exe_ver:08x}, .bin @ 0x{fw_ver:08x}")
    print(f"  rel(WMCU - MYDW-AV) in .exe = {exe_wmcu - exe_ver:+d}")
    print(f"  rel(WMCU - MYDW-AV) in .bin = {fw_wmcu - fw_ver:+d}")
    print()

    # Align the .exe region with .bin around the WMCU header and compare.
    # In the .bin, MYDW-AV-V1.06 is at fw_ver; in the .exe it's at exe_ver.
    # If the firmware is embedded verbatim, then .exe[exe_ver - fw_ver : ...]
    # should == fw.
    bin_start_in_exe = exe_ver - fw_ver
    if bin_start_in_exe < 0:
        print("  Anchor anchor MYDW-AV is closer to file start in .exe than in .bin — abort.")
    else:
        end = bin_start_in_exe + len(fw)
        if end <= len(exe):
            slab = exe[bin_start_in_exe:end]
            same = slab == fw
            print(f"Embedded fw test: .exe[{bin_start_in_exe:#x}:{end:#x}] == .bin? {same}")
            if same:
                print(f"  ✓ The whole {len(fw):,}-byte firmware is embedded verbatim.")
            else:
                # find first divergence
                first_diff = next((i for i in range(len(slab)) if slab[i] != fw[i]), None)
                last_diff = next((i for i in range(len(slab)-1, -1, -1)
                                 if slab[i] != fw[i]), None)
                eq_count = sum(1 for a, b in zip(slab, fw) if a == b)
                print(f"  ✗ Bytes equal: {eq_count}/{len(fw)} "
                      f"({eq_count*100/len(fw):.1f}%)")
                print(f"  first diff @ rel offset 0x{first_diff:x},"
                      f" last diff @ rel offset 0x{last_diff:x}")

    # Carve a wide region around the .exe WMCU site so the user can inspect
    # what surrounds it (might reveal pre/post template bytes the app
    # composes the final WMCU with).
    win = 4096
    around_start = max(0, exe_wmcu - win)
    around_end = min(len(exe), exe_wmcu + len(fw) + win)
    OUT_FW.write_bytes(exe[around_start:around_end])
    print(f"\nCarved {OUT_FW.name}: bytes 0x{around_start:x}..0x{around_end:x} "
          f"({around_end - around_start:,} bytes)")
    print("  WMCU magic sits at relative offset "
          f"0x{exe_wmcu - around_start:x} inside the carved blob.")

    # Print first 64 bytes around the WMCU header in both files for quick eyeball
    print("\n.exe bytes around WMCU header:")
    print("  " + exe[exe_wmcu - 16: exe_wmcu + 48].hex(" "))
    print(".bin bytes around WMCU header:")
    print("  " + fw[fw_wmcu - 16: fw_wmcu + 48].hex(" "))


if __name__ == "__main__":
    main()
