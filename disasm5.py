#!/usr/bin/env python3
"""
1. Read the vector table to find Reset_Handler → main()
2. Disassemble 0x08007efc (called after HID OUT packet received)
3. Disassemble 0x08009ff0 (tail-called from HID OUT handler)
4. Look for the command-byte dispatch table
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

def resolve_ldr_pc(ins):
    try:
        if '[pc' not in ins.op_str.lower():
            return None
        imm_str = ins.op_str.split('#')[1].rstrip(']').strip()
        imm = int(imm_str, 0)
        ref_fa = ((ins.address + 4) & ~3) + imm
        ref_off = fa2off(ref_fa)
        if 0 <= ref_off < len(fw) - 3:
            return struct.unpack_from('<I', fw, ref_off)[0]
    except Exception:
        pass
    return None

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
md.detail = True

def disasm_fn(start_fa, max_bytes=512, label=''):
    off = fa2off(start_fa)
    if off < 0 or off + 4 > len(fw):
        print(f'\n[SKIP] {label} @ {start_fa:#010x} out of range')
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
        if seen_push and n > 2:
            if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
                break
            if ins.mnemonic == 'pop' and 'pc' in ins.op_str:
                break

# ── Vector table ────────────────────────────────────────────────
print('─── Vector table (first 8 entries) ───')
vt_off = FILE_HEADER  # vector table at start of app image = flash 0x08005000
for i in range(8):
    v = struct.unpack_from('<I', fw, vt_off + i*4)[0]
    names = ['Initial_SP', 'Reset_Handler', 'NMI', 'HardFault',
             'MemManage', 'BusFault', 'UsageFault', 'Reserved']
    print(f'  [{i:2d}] {names[i]:<20s} 0x{v:08x}  (code at 0x{v & ~1:08x})')

reset_handler = struct.unpack_from('<I', fw, vt_off + 4)[0] & ~1
print(f'\nReset_Handler → 0x{reset_handler:08x}')

# ── Reset_Handler ────────────────────────────────────────────────
disasm_fn(reset_handler, 256, 'Reset_Handler')

# ── After packet received ────────────────────────────────────────
disasm_fn(0x0800_7efc, 512, 'fn_7efc (called after HID OUT packet)')
disasm_fn(0x0800_9ff0, 256, 'fn_9ff0 (tail-called from OUT handler)')

# ── Look for command dispatch: TBB/TBH (table branch byte/halfword) ──
print('\n─── TBB/TBH instructions (switch tables) in firmware ───')
code_start = fa2off(0x08005000)
code_end   = min(len(fw), fa2off(0x08010000) + FILE_HEADER) if fa2off(0x08010000) < len(fw) else len(fw)
chunk = fw[code_start:code_end]

for ins in md.disasm(chunk, 0x08005000):
    if ins.mnemonic in ('tbb', 'tbh'):
        print(f'  {ins.address:#010x}: {ins.mnemonic} {ins.op_str}')

# ── Look for LDRB followed by multi-CMP patterns ──
print('\n─── LDRB + CMP dispatch patterns ───')
insns = list(md.disasm(chunk, 0x08005000))
for i, ins in enumerate(insns):
    if ins.mnemonic == 'ldrb' and i + 30 < len(insns):
        # Count CMPs in next 30 instructions
        n_cmp = sum(1 for j in range(1, 30) if insns[i+j].mnemonic == 'cmp')
        if n_cmp >= 4:
            print(f'\n  DISPATCH at {ins.address:#010x}: ldrb {ins.op_str}  ({n_cmp} CMPs follow)')
            for j in range(0, min(40, len(insns)-i)):
                ni = insns[i+j]
                ann = ''
                v = resolve_ldr_pc(ni)
                if v: ann = f'  ; = 0x{v:08x}'
                print(f'    {ni.address:#010x}:  {ni.mnemonic:<8s} {ni.op_str}{ann}')
                if ni.mnemonic in ('pop',) and 'pc' in ni.op_str and j > 10:
                    break
                if ni.mnemonic == 'bx' and 'lr' in ni.op_str and j > 10:
                    break
