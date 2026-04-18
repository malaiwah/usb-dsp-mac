#!/usr/bin/env python3
"""
1. Find what writes to 0x200000d4/0x200000d8 (USB vtable pointers set at runtime)
2. Disassemble fn_c114 fully (USB HID command dispatcher) 
3. Disassemble fn_b45c (HID endpoint setup)
4. Look at all code that stores to 0x200000d4-0x200000e0 SRAM area
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

# fn_c114 full disassembly - USB HID command processor
disasm_fn(0x0800c114, 600, 'fn_c114 HID cmd processor', stop_early=False)

# fn_b45c - HID endpoint setup (called from USB full init)
disasm_fn(0x0800b45c, 400, 'fn_b45c HID EP setup', stop_early=False)

# Now search for what writes to 0x200000d4 area
print('\n─── Search for writes to 0x200000d4 area (USB vtable pointers) ───')
all_code = fw[FILE_HEADER:]
all_insns = list(md.disasm(all_code, FLASH_BASE))

for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('ldr', 'ldr.w'):
        val = resolve_ldr_pc(ins)
        if val and 0x200000d0 <= val <= 0x200000f0:
            reg = ins.op_str.split(',')[0].strip()
            print(f'\n  LDR SRAM-vtable-area({val:#x}) at {ins.address:#010x}, reg={reg}')
            for j in range(max(0,i-5), min(len(all_insns), i+15)):
                ni = all_insns[j]
                v2 = resolve_ldr_pc(ni)
                vann = f'  ; = 0x{v2:08x}' if v2 else ''
                if v2 and 0x20000000 <= v2 <= 0x20010000: vann += ' ← SRAM'
                if v2 and 0x08005000 <= v2 <= 0x08016298: vann += ' ← FLASH'
                mark = ' ←' if j==i else ''
                print(f'    {ni.address:#010x}: {ni.mnemonic} {ni.op_str}{vann}{mark}')

# Also look for any STR that stores flash addresses to SRAM
# The USB init code should do: ldr r1, =#<handler_fn>; str r1, [r0, #...] with r0 = 0x200000d4 or similar
# Look for STR instructions where the source register was loaded from a flash constant
print('\n─── Search for stores of flash addresses (function pointers) to SRAM ───')
# Track what value each register holds (simplified - look for ldr+str pairs)
for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('ldr', 'ldr.w'):
        val = resolve_ldr_pc(ins)
        if val and 0x08005001 <= val <= 0x08016001 and (val & 1):  # Thumb fn ptr
            reg = ins.op_str.split(',')[0].strip()
            # Look for nearby STR
            for j in range(i+1, min(len(all_insns), i+8)):
                ni = all_insns[j]
                if ni.mnemonic.startswith('str') and reg in ni.op_str:
                    # Check if target base register was loaded from 0x200000d4 area
                    base_reg = ni.op_str.split('[')[1].split(',')[0].strip().rstrip(']')
                    # Look backwards for where base_reg was set
                    for k in range(max(0,i-15), i):
                        nk = all_insns[k]
                        v2 = resolve_ldr_pc(nk)
                        dst = nk.op_str.split(',')[0].strip()
                        if dst == base_reg and v2 and 0x200000c0 <= v2 <= 0x20000110:
                            print(f'  FN-PTR STORE: flash {val:#010x} → SRAM via {base_reg}={v2:#x} at {ni.address:#010x}')
                            print(f'    loaded at {nk.address:#010x}: {nk.mnemonic} {nk.op_str}  ; ={v2:#010x}')
                            print(f'    store at  {ni.address:#010x}: {ni.mnemonic} {ni.op_str}')
                    break

