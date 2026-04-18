#!/usr/bin/env python3
"""
Trace from main() through USB init to find the HID endpoint handlers.
Also look for the actual HID packet processor (command byte dispatcher).
"""
import struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

FIRMWARE = '/Users/mbelleau/Code/usb_dsp_mac/downloads/DSP-408-Firmware.bin'
FLASH_BASE = 0x08005000
FILE_HEADER = 8

with open(FIRMWARE, 'rb') as f:
    fw = f.read()

def fa2off(addr):
    return (addr - FLASH_BASE) + FILE_HEADER

def off2fa(off):
    return (off - FILE_HEADER) + FLASH_BASE

def read32(fa):
    off = fa2off(fa)
    if 0 <= off < len(fw)-3:
        return struct.unpack_from('<I', fw, off)[0]
    return None

def resolve_ldr_pc(ins):
    try:
        if '[pc' not in ins.op_str.lower():
            return None
        imm_str = ins.op_str.split('#')[1].rstrip(']').strip()
        imm = int(imm_str, 0)
        ref_fa = ((ins.address + 4) & ~3) + imm
        return read32(ref_fa)
    except Exception:
        pass
    return None

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
md.detail = True

def disasm_fn(start_fa, max_bytes=400, label='', stop_at_ret=True):
    off = fa2off(start_fa)
    if off < 0 or off + 4 > len(fw):
        print(f'\n[SKIP] {label} @ {start_fa:#010x}')
        return
    code = fw[off:off+max_bytes]
    print(f'\n{"═"*72}')
    print(f'  {label}  @  {start_fa:#010x}')
    print(f'{"═"*72}')
    seen_push = False
    n = 0
    for ins in md.disasm(code, start_fa):
        ann = ''
        val = resolve_ldr_pc(ins)
        if val is not None:
            ann = f'  ; = 0x{val:08x}'
        print(f'  {ins.address:#010x}:  {ins.bytes.hex():<12s}  {ins.mnemonic:<8s} {ins.op_str}{ann}')
        if 'push' in ins.mnemonic:
            seen_push = True
        n += 1
        if stop_at_ret and seen_push and n > 2:
            if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
                break
            if ins.mnemonic == 'pop' and 'pc' in ins.op_str:
                break

# main() is at 0x080050ed
disasm_fn(0x080050ed, 800, 'main()')

# USB init called from 0x08006066
disasm_fn(0x0800a030, 400, 'USB_init @ a030')

# Look for all STR instructions that write to 0x20000074 range
# by searching for patterns: ldr rx, =0x2000007x; str ry, [rx]
print('\n─── Searching for writes to EP handler tables (0x20000074, 0x20000090) ───')
all_insns = list(md.disasm(fw[FILE_HEADER:], FLASH_BASE))
for i, ins in enumerate(all_insns):
    if ins.mnemonic == 'ldr':
        val = resolve_ldr_pc(ins)
        if val in (0x20000074, 0x20000090, 0x20000078, 0x2000007c, 0x20000080,
                   0x20000094, 0x20000098, 0x2000009c):
            # Look at surrounding instructions
            print(f'\n  Found ref to EP table at {ins.address:#010x}:')
            for j in range(max(0,i-3), min(len(all_insns), i+8)):
                ni = all_insns[j]
                v = resolve_ldr_pc(ni)
                vann = f'  ; = 0x{v:08x}' if v else ''
                mark = ' ←' if j==i else ''
                print(f'    {ni.address:#010x}: {ni.mnemonic} {ni.op_str}{vann}{mark}')

# Also search for the actual HID data packet handler
# The 64-byte HID report should land at some SRAM address
# Find all code that does strh/strb in a loop with count 64 (0x40) or 32 pairs
print('\n─── Looking for PMA→SRAM copy loops (HID data reception) ───')
for i, ins in enumerate(all_insns):
    if ins.mnemonic == 'cmp' and '0x40' in ins.op_str:
        print(f'  CMP 0x40 at {ins.address:#010x}: {ins.op_str}')
        # Show context
        for j in range(max(0,i-5), min(len(all_insns), i+5)):
            ni = all_insns[j]
            mark = ' ←' if j==i else ''
            print(f'    {ni.address:#010x}: {ni.mnemonic} {ni.op_str}{mark}')
        print()
