#!/usr/bin/env python3
"""
Disassemble 0x0800cc84 (the full USB init that sets up endpoints and handlers).
Also look for the HID data handler and the actual command dispatcher.
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

def disasm_fn(start_fa, max_bytes=600, label=''):
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
        if seen_push and n > 2:
            if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
                break
            if ins.mnemonic == 'pop' and 'pc' in ins.op_str:
                break

# The full USB init
disasm_fn(0x0800cc84, 600, 'USB_full_init @ cc84')

# Find main() properly — it's at the actual start address from Reset_Handler
# Reset_Handler at 0x08005100 calls:
# 1. 0x0800bd6d (C startup)
# 2. 0x080050ed as BX → Thumb code at 0x080050ec

# Let me read what's at 0x080050ec in the file
off = fa2off(0x080050ec)
print(f'\n─── Bytes at main candidate 0x080050ec (file offset {off:#x}) ───')
print(' '.join(f'{b:02x}' for b in fw[off:off+32]))

# Try Reset_Handler more carefully
# The LDR r0, =0x080050ed is at address 0x08005104
# The LDR constants are at file offset:
# 0x08005100+4+4 = 0x08005108 for first constant (+8 from first LDR PC-relative)
# Actually the constants are:
# ldr r0, [pc, #0x18] at 0x08005100 → [pc+0x18] = [(0x08005100+4)&~3 + 0x18] = [0x08005104+0x18] = [0x0800511c]
# ldr r0, [pc, #0x18] at 0x08005104 → [(0x08005104+4)&~3 + 0x18] = [0x08005120]
v1 = read32(0x0800511c)
v2 = read32(0x08005120)
print(f'\n  Reset_Handler constant 1 @ 0x0800511c: 0x{v1:08x}  (C startup? = 0x{v1&~1:08x})')
print(f'  Reset_Handler constant 2 @ 0x08005120: 0x{v2:08x}  (main?       = 0x{v2&~1:08x})')

main_fa = v2 & ~1 if v2 else 0x080050ec
print(f'\n  → Attempting disasm of main() at 0x{main_fa:08x}')
disasm_fn(main_fa, 800, f'main() @ 0x{main_fa:08x}')

# Also find the HID RX handler by looking at what writes to SRAM in the init
# Search for STR instructions with source being a function-like address
print('\n─── Thumb function addresses stored as constants in firmware ───')
# Scan for 32-bit values that look like Thumb code pointers (bit0=1, in flash range)
code_pointers = set()
for i in range(FILE_HEADER, len(fw)-4, 4):
    v = struct.unpack_from('<I', fw, i)[0]
    if (v & 1) and (0x08005001 <= v <= 0x08011001):
        fa = v - FILE_HEADER + FLASH_BASE - 4  # where this is stored
        code_pointers.add((v & ~1, v, i - FILE_HEADER + FLASH_BASE))

print(f'  Found {len(code_pointers)} potential Thumb code pointers')
# Print ones that appear to be in early init code (small offset from flash base)
# and are referenced from addresses near USB init
print('\n─── Pointers stored in first 4KB of flash (likely table entries) ───')
for target, raw, stored_at in sorted(code_pointers, key=lambda x: x[2]):
    if stored_at < 0x08006000:  # first 4KB after flash base
        print(f'  stored @ {stored_at:#010x}: = 0x{raw:08x} → target 0x{target:08x}')
