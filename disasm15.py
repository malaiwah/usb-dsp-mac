#!/usr/bin/env python3
"""
1. Disassemble fn_5d8c - checksum function (where IS the checksum in the buffer?)
2. Find the HID OUT reception handler (what sets 0x2000016f flag?)
3. Look at fn_ab8c (called for USB WKUP/RESET events, r0=0)
4. Look at fn_a2e8 (EP config) and fn_a204 (EP type setup) 
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
        if val and 0x20000000 <= val <= 0x20010000:
            ann += ' ← SRAM'
        if val and 0x08005000 <= val <= 0x08016298:
            ann += ' ← FLASH'
        print(f'  {ins.address:#010x}:  {ins.bytes.hex():<12s}  {ins.mnemonic:<8s} {ins.op_str}{ann}')
        if 'push' in ins.mnemonic:
            seen_push = True
        n += 1
        if stop_early and seen_push and n > 4:
            if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
                break
            if ins.mnemonic == 'pop' and 'pc' in ins.op_str:
                break

# Checksum function - CRITICAL: tells us where checksum lives in the packet
disasm_fn(0x08005d8c, 200, 'checksum fn @ 5d8c', stop_early=False)

# USB event handler - called for WKUP (r0=0), WKUP (r0=2), ESOF (r0=7)
disasm_fn(0x0800ab8c, 300, 'USB_event_handler @ ab8c', stop_early=False)

# fn_a2e8 - EP config (called from fn_b45c HID EP setup with r0=0)
disasm_fn(0x0800a2e8, 200, 'fn_a2e8 EP config', stop_early=False)

# Search for what sets flag at 0x2000016f (byte at offset 5 of 0x2000016a)
# Looking for STRB instructions that store to [r?, #5] where r? was loaded from 0x2000016a
print('\n─── Search for STRB instructions near 0x2000016a+5 ───')
all_code = fw[FILE_HEADER:]
all_insns = list(md.disasm(all_code, FLASH_BASE))

# Build a map of what each instruction address does for register tracking
# Look for pattern: ldr rX, =#0x2000016a; ... strb rY, [rX, #5]
for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('strb', 'strb.w'):
        op = ins.op_str
        # Check for offset #5, #0x5
        if '#5]' in op or '#0x5]' in op or '#5,' in op:
            base_reg = op.split('[')[1].split(',')[0].strip().rstrip(']')
            # Look back for ldr of 0x2000016a into base_reg
            for j in range(max(0,i-30), i):
                nj = all_insns[j]
                v = resolve_ldr_pc(nj)
                dst = nj.op_str.split(',')[0].strip()
                if v == 0x2000016a and dst == base_reg:
                    print(f'\n  Found STRB at offset 5 from 0x2000016a!')
                    for k in range(max(0,j-2), min(len(all_insns),i+4)):
                        nk = all_insns[k]
                        v2 = resolve_ldr_pc(nk)
                        vann = f'  ; = 0x{v2:08x}' if v2 else ''
                        mark = ' ←' if k==i else (' [ldr]' if k==j else '')
                        print(f'    {nk.address:#010x}: {nk.mnemonic} {nk.op_str}{vann}{mark}')

# Also search for STR r?, [r?, #5] (word store to offset 5 - unlikely for a byte flag)  
# Plus search using the byte-band address approach

# Broader: find any STRB where the value being stored is 1, near code that also refs 0x2000016a
print('\n─── All LDR of 0x2000016a in full firmware ───')
for i, ins in enumerate(all_insns):
    val = resolve_ldr_pc(ins)
    if val == 0x2000016a:
        print(f'  {ins.address:#010x}: {ins.mnemonic} {ins.op_str}  ; = 0x2000016a')
        # Show surrounding context
        for j in range(max(0,i-2), min(len(all_insns), i+20)):
            nj = all_insns[j]
            v2 = resolve_ldr_pc(nj)
            vann = f'  ; = 0x{v2:08x}' if v2 else ''
            mark = ' ←' if j==i else ''
            print(f'    {nj.address:#010x}: {nj.mnemonic} {nj.op_str}{vann}{mark}')
        print()

