#!/usr/bin/env python3
"""
Disassemble the HID OUT packet handler (host→device data).
Also traces what happens to the received payload.
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

def disasm_fn(start_fa, max_bytes=600, label=''):
    off = fa2off(start_fa)
    if off < 0 or off >= len(fw):
        print(f'\n[SKIP] {label} @ {start_fa:#010x} out of file range')
        return
    code = fw[off:off+max_bytes]
    print(f'\n{"═"*72}')
    print(f'  {label}  @  {start_fa:#010x}  (file +{off:#x})')
    print(f'{"═"*72}')
    seen_push = False
    for ins in md.disasm(code, start_fa):
        ann = ''
        val = resolve_ldr_pc(ins)
        if val is not None:
            ann = f'  ; = 0x{val:08x}'
        print(f'  {ins.address:#010x}:  {ins.bytes.hex():<12s}  {ins.mnemonic:<8s} {ins.op_str}{ann}')
        if 'push' in ins.mnemonic:
            seen_push = True
        if seen_push:
            if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
                break
            if ins.mnemonic == 'pop' and 'pc' in ins.op_str:
                break

# 0x0800b470 — called when EP OUT fires (bit 14 of ISTR set, direction=OUT)
# This is likely the HID OUT handler: receives host→device 64-byte packet
disasm_fn(0x0800b470, 512, 'HID OUT handler (EP1 OUT? host→device)')

# 0x08009f10 — another EP handler
disasm_fn(0x08009f10, 512, 'EP handler @ 9f10')

# USB init function — sets up those function pointer tables
# Referenced from 0x08006000 area
disasm_fn(0x08006000, 256, 'USB_init? @ 6000')

# The checksum function we spotted at 0x08005d8c — reads from 0x2000050e
# That buffer at 0x2000050e likely holds the received HID packet
print('\n─── Buffer at 0x2000050e (receiver buffer referenced) ───')
print('  This SRAM address holds the received HID packet data.')
print('  offset +4 = first data byte, +5, +6... = rest of packet')
print('  The checksum fn at 0x08005d8c XORs bytes from [+4..+last]')

# Also look at the function at 0x0800b470 more context:
# Let's find what function initialises the USB OUT endpoint callback
# by searching for 0x0800b470 as a 32-bit constant in flash
target = struct.pack('<I', 0x0800b471)  # Thumb address (bit0 set)
matches = []
for i in range(0, len(fw)-4, 2):
    if fw[i:i+4] == target:
        matches.append(i)
if matches:
    print(f'\n─── References to fn 0x0800b471 (Thumb) in flash ───')
    for m in matches:
        fa = m - FILE_HEADER + FLASH_BASE
        print(f'    file_offset={m:#x} → flash={fa:#010x}')
        # Show surrounding context
        off2 = max(0, m-8)
        ctx = fw[off2:m+8]
        for ins in md.disasm(ctx, fa - (m - off2)):
            print(f'      {ins.address:#010x}: {ins.mnemonic} {ins.op_str}')
else:
    print(f'\n  No direct references to 0x0800b471 found')
    # Try even address
    target2 = struct.pack('<I', 0x0800b470)
    matches2 = [i for i in range(0, len(fw)-4, 2) if fw[i:i+4] == target2]
    if matches2:
        print(f'  References to 0x0800b470 (non-Thumb): {[hex(m) for m in matches2]}')

# Let's look at fn_0800ab8c more carefully — it's called for USB events
# More importantly, what sets the function pointer at 0x200000d4+4?
# That's called as BLX when RESET interrupt fires.
# Search for stores to 0x200000d4..0x200000d8
print('\n─── USB init: what writes to the CTR function pointer table ───')
# The table at 0x20000074 (RX handlers) and 0x20000090 (TX handlers)
# Let's find where 0x20000074 is written
for target_addr, label in [(0x20000074, 'RX_handler_table'), (0x20000090, 'TX_handler_table')]:
    target = struct.pack('<I', target_addr)
    matches = [i for i in range(0, len(fw)-4, 2) if fw[i:i+4] == target]
    print(f'\n  {label} (0x{target_addr:08x}) referenced at:')
    for m in matches:
        fa = m - FILE_HEADER + FLASH_BASE
        print(f'    flash={fa:#010x}  (file {m:#x})')
