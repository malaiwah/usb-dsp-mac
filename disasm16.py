#!/usr/bin/env python3
"""
1. fn_7d84 - called during USB WKUP handling, probably sets up EP tables 
2. fn_bd04 - called during USB RESET when device already active
3. Search ALL strb with offset #5 from any reg (find who sets 0x2000016f)
4. Disassemble functions near the CTR handler that copy PMA → SRAM
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

disasm_fn(0x0800_7d84, 400, 'fn_7d84 (USB init/reset?)', stop_early=False)
disasm_fn(0x0800bd04, 200, 'fn_bd04 (USB reset active)', stop_early=False)

# Search for ALL strb/strb.w that use offset #5 from any register
print('\n─── All STRB with immediate offset 5 ───')
all_code = fw[FILE_HEADER:]
all_insns = list(md.disasm(all_code, FLASH_BASE))

for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('strb', 'strb.w'):
        op = ins.op_str
        if '#5]' in op or '#0x5]' in op:
            # Show context
            print(f'\n  STRB offset 5 @ {ins.address:#010x}: {ins.mnemonic} {op}')
            # Print surrounding context with any SRAM refs
            for j in range(max(0,i-10), min(len(all_insns),i+3)):
                nj = all_insns[j]
                v = resolve_ldr_pc(nj)
                vann = f'  ; = 0x{v:08x}' if v else ''
                if v and 0x20000000 <= v <= 0x20010000: vann += ' ← SRAM'
                mark = ' ← HERE' if j==i else ''
                print(f'    {nj.address:#010x}: {nj.mnemonic} {nj.op_str}{vann}{mark}')

# Also search for loads of the specific SRAM address 0x2000016f
print('\n─── LDR of 0x2000016f directly ───')
target = struct.pack('<I', 0x2000016f)
for i in range(FILE_HEADER, len(fw)-4, 4):
    if fw[i:i+4] == target:
        fa = i - FILE_HEADER + FLASH_BASE
        print(f'  Constant 0x2000016f at flash {fa:#010x}')

# Search for the PMA→SRAM copy that fills the HID OUT buffer at 0x2000050e
# The PMA base is 0x40006000. Code that reads from PMA and writes to 0x2000050e
print('\n─── LDR of PMA base (0x40006000) near SRAM refs to 0x2000050e ───')
for i, ins in enumerate(all_insns):
    val = resolve_ldr_pc(ins)
    if val == 0x40006000:
        # check if 0x2000050e appears in surrounding ~40 instructions
        for j in range(max(0,i-40), min(len(all_insns), i+40)):
            v2 = resolve_ldr_pc(all_insns[j])
            if v2 and (0x2000050e == v2 or (0x2000050e <= v2 <= 0x2000060e)):
                print(f'\n  PMA+SRAM match @ {ins.address:#010x}:')
                for k in range(max(0,min(i,j)-5), min(len(all_insns),max(i,j)+5)):
                    nk = all_insns[k]
                    v3 = resolve_ldr_pc(nk)
                    vann = f'  ; = 0x{v3:08x}' if v3 else ''
                    print(f'    {nk.address:#010x}: {nk.mnemonic} {nk.op_str}{vann}')
                break

