#!/usr/bin/env python3
"""
Trace USB endpoint init and find the HID RX/TX handlers.
Also look at USART1 handler (0x0800ca08) for serial protocol clues.
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

def disasm_fn(start_fa, max_bytes=400, label='', stop_early=True):
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

# USB endpoint init
disasm_fn(0x0800ccd0, 300, 'USB_ep_init @ ccd0')
disasm_fn(0x0800b45c, 200, 'fn_b45c (called from USB full init)')

# USART1 handler — may reveal serial protocol
disasm_fn(0x0800ca08, 400, 'USART1_IRQHandler @ ca08')
disasm_fn(0x0800ca34, 300, 'USART2_IRQHandler @ ca34')

# PendSV (RTOS task switch?)
disasm_fn(0x0800_9fec, 200, 'PendSV @ 9fec')

# The function 0x0800c114 (called from main loop)
disasm_fn(0x0800c114, 400, 'fn_c114 (main loop call 1)')

# The function 0x0800d8d8 (called from main loop)
disasm_fn(0x0800d8d8, 400, 'fn_d8d8 (main loop call 2)')

# Now let's find HID RX handler: scan for stores into 0x20000074..0x200000b0
# using any indirect addressing
print('\n─── Scan all STR instructions for writes to EP table SRAM region ───')
all_code = fw[FILE_HEADER:]
all_insns = list(md.disasm(all_code, FLASH_BASE))

# Look for STR with small offset from a base that could be 0x20000000
# Or look for: ldr rX, =#0x20000074; str rY, [rX]
for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('str', 'str.w', 'strh', 'strb'):
        # Check if the address could be in 0x20000070..0x200000b0
        # Look for pattern: ldr rX = 0x200000??; str rY, [rX, #n]
        pass
    # Better: look for any ldr that loads a value in our range
    if ins.mnemonic in ('ldr', 'ldr.w'):
        val = resolve_ldr_pc(ins)
        if val and 0x20000070 <= val <= 0x200000b0:
            print(f'\n  LDR of EP-table addr at {ins.address:#010x}: = 0x{val:08x}')
            # Print surrounding context
            for j in range(max(0,i-3), min(len(all_insns), i+8)):
                ni = all_insns[j]
                v = resolve_ldr_pc(ni)
                vann = f'  ; = 0x{v:08x}' if v else ''
                mark = ' ←' if j==i else ''
                print(f'    {ni.address:#010x}: {ni.mnemonic} {ni.op_str}{vann}{mark}')
