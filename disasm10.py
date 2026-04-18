#!/usr/bin/env python3
"""
Find the actual HID OUT packet handler by:
1. Looking at USB endpoint setup (0x08007e54)
2. Searching for what writes to the EP handler SRAM tables
3. Looking at the checksum/packet validation function at 0x08005d8c context
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
        print(f'  {ins.address:#010x}:  {ins.bytes.hex():<12s}  {ins.mnemonic:<8s} {ins.op_str}{ann}')
        if 'push' in ins.mnemonic:
            seen_push = True
        n += 1
        if stop_early and seen_push and n > 4:
            if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
                break
            if ins.mnemonic == 'pop' and 'pc' in ins.op_str:
                break

# USB endpoint setup
disasm_fn(0x08007e54, 400, 'USB_ep_setup @ 7e54')

# fn_c8cc — is this USART TX or USB HID send?
disasm_fn(0x0800c8cc, 200, 'fn_c8cc (TX byte?)')

# fn_0800a2e8 — USB endpoint config
disasm_fn(0x0800a2e8, 300, 'USB_ep_config @ a2e8')

# fn_0800a204 — endpoint type setup
disasm_fn(0x0800a204, 300, 'USB_ep_type @ a204')

# What receives the HID OUT data? Look for anything that reads 64 bytes
# and sets the flag at 0x2000016f (= 0x2000016a + 5)
print('\n─── Searching for writes to flag 0x2000016f ───')
all_code = fw[FILE_HEADER:]
all_insns = list(md.disasm(all_code, FLASH_BASE))

for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('ldr', 'ldr.w'):
        val = resolve_ldr_pc(ins)
        if val == 0x2000016a:
            # Look for nearby strb with small offset
            print(f'\n  LDR 0x2000016a at {ins.address:#010x}')
            for j in range(max(0,i-2), min(len(all_insns), i+12)):
                ni = all_insns[j]
                v = resolve_ldr_pc(ni)
                vann = f'  ; = 0x{v:08x}' if v else ''
                mark = ' ←' if j==i else ''
                print(f'    {ni.address:#010x}: {ni.mnemonic} {ni.op_str}{vann}{mark}')

# Also look for what stores to 0x2000016a directly
print('\n─── Searching for writes to 0x200001?? region (HID state machine) ───')
for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('ldr', 'ldr.w'):
        val = resolve_ldr_pc(ins)
        if val and 0x20001600 <= val <= 0x200016ff:
            reg = ins.op_str.split(',')[0].strip()
            # Check next few insns for stores using this reg
            for j in range(i+1, min(len(all_insns), i+6)):
                ni = all_insns[j]
                if reg in ni.op_str and ni.mnemonic.startswith('str'):
                    print(f'  [{ins.address:#010x}] STORE via {val:#010x}: {ni.address:#010x}: {ni.mnemonic} {ni.op_str}')

# Search for references to 0x2000050e (the main packet buffer)
print('\n─── References to packet buffer 0x2000050e ───')
target = struct.pack('<I', 0x2000050e)
for i in range(FILE_HEADER, len(fw)-4, 4):
    if fw[i:i+4] == target:
        fa = i - FILE_HEADER + FLASH_BASE
        print(f'  Referenced at flash {fa:#010x} (file {i:#x})')
        # Show surrounding context
        off2 = max(FILE_HEADER, i-12)
        ctx = fw[off2:i+16]
        ctx_fa = off2 - FILE_HEADER + FLASH_BASE
        for ins in md.disasm(ctx, ctx_fa):
            print(f'    {ins.address:#010x}: {ins.mnemonic} {ins.op_str}')
