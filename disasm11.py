#!/usr/bin/env python3
"""
Find the HID OUT 64-byte report assembler and the full command dispatch.
Also decode the TBB table in fn_a2f4 for HID sub-commands.
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

def read32(fa):
    off = fa2off(fa)
    if 0 <= off < len(fw)-3:
        return struct.unpack_from('<I', fw, off)[0]
    return None

def read8(fa):
    off = fa2off(fa)
    if 0 <= off < len(fw):
        return fw[off]
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

def disasm_fn(start_fa, max_bytes=500, label='', stop_early=True):
    off = fa2off(start_fa)
    if off < 0 or off + 4 > len(fw):
        print(f'\n[SKIP] {label} @ {start_fa:#010x}')
        return
    code = fw[off:off+max_bytes]
    print(f'\n{"═"*72}')
    print(f'  {label}  @  {start_fa:#010x}  (file +{off:#x})')
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
        if stop_early and seen_push and n > 4:
            if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
                break
            if ins.mnemonic == 'pop' and 'pc' in ins.op_str:
                break

# The TBB dispatch in fn_a2f4 at 0x0800a330 when r1=9 and r4=sub_cmd
# First let's decode the TBB table
print('─── TBB table at 0x0800a334 (sub-commands for r1=9, 0-20) ───')
tbb_base = 0x0800a332  # instruction address of tbb [pc, r4]
# TBB: next insn addr = (PC + offset_table[r4]) * 2
# PC for TBB is tbb_addr + 4
pc = tbb_base + 4
tbb_table_fa = pc  # table starts right after the TBB instruction
for i in range(22):  # 0..21
    b = read8(tbb_table_fa + i)
    if b is not None:
        target = pc + b * 2
        print(f'  sub_cmd[{i:2d}] offset={b:#04x} → target 0x{target:08x}')

# The fn_a2f4 with full context for r1=9
disasm_fn(0x0800a320, 300, 'fn_a2f4 r1=9 branch')

# The USB HID sender
disasm_fn(0x0800cdf8, 400, 'USB_HID_send @ cdf8')
disasm_fn(0x08005a50, 400, 'USB_HID_send2 @ 5a50')

# The "HID receive assembler" — look for functions that reference the 0x2000050e buffer
# and that increment a counter / check for completeness
# Let's look at what refers to 0x2000016a (the main state struct)
print('\n─── Searching for code that sets flag 0x2000016f (bit 5 of 0x2000016a) ───')
all_code = fw[FILE_HEADER:]
all_insns = list(md.disasm(all_code, FLASH_BASE))

# Find strb (store byte) instructions at small offsets from 0x2000016a
for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('strb', 'strb.w'):
        # Look back for ldr of 0x2000016a
        for j in range(max(0,i-20), i):
            nj = all_insns[j]
            if resolve_ldr_pc(nj) == 0x2000016a:
                # Check if the strb is at offset 5
                if '#5' in ins.op_str or ', 5]' in ins.op_str or '#0x5' in ins.op_str:
                    print(f'\n  STRB at offset 5 from 0x2000016a @ {ins.address:#010x}: {ins.op_str}')
                    for k in range(max(0,i-8), min(len(all_insns),i+4)):
                        nk = all_insns[k]
                        v = resolve_ldr_pc(nk)
                        vann = f'  ; = 0x{v:08x}' if v else ''
                        mark = ' ←' if k==i else ''
                        print(f'    {nk.address:#010x}: {nk.mnemonic} {nk.op_str}{vann}{mark}')

# Also look for what stores 1 to offset +5 of any SRAM pointer loaded as 0x2000016a
print('\n─── All references to 0x2000016a in flash ───')
target = struct.pack('<I', 0x2000016a)
for i in range(FILE_HEADER, len(fw)-4, 4):
    if fw[i:i+4] == target:
        fa = i - FILE_HEADER + FLASH_BASE
        print(f'  at flash {fa:#010x}')

# Look at 0x0800cfc8 (called for HID 0xA1 command)
disasm_fn(0x0800cfc8, 500, 'HID_cmd_A1_handler @ cfc8')
