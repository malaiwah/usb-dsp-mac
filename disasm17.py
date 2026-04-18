#!/usr/bin/env python3
"""
1. Binary-level search for STRB instructions with offset 5 in firmware
2. Look at fn_66f8 (tail-called from fn_bd04 USB suspend/resume)
3. Disassemble the CTR handler + surrounding area for HID OUT RX function
4. Look at fn_ccd0 USB EP state init more carefully
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

# Binary search for STRB Rd, [Rn, #5] instructions
# Thumb encoding: 0111 0 00101 nnn ddd → high byte = 0x71, low byte = 0x4n where n encodes Rn, Rd
# low byte = 0100 0rrr where rrr would be different for each Rn, Rd combo
# More precisely: halfword = 0111 0001 01 NNN DDD (big-endian view)
# In LE file storage: byte[0] = (0x40 | (Rn << 3) | Rd), byte[1] = 0x71
# So we search for byte pairs where byte[1]=0x71 and byte[0] in 0x40..0x7f, at even offsets
print('─── Binary search: STRB Rd, [Rn, #5] instructions in firmware ───')
strb5_found = []
for i in range(FILE_HEADER, len(fw)-1, 2):  # Even alignment only
    b0 = fw[i]
    b1 = fw[i+1]
    if b1 == 0x71 and 0x40 <= b0 <= 0x7f:
        fa = (i - FILE_HEADER) + FLASH_BASE
        rn = (b0 >> 3) & 7
        rd = b0 & 7
        strb5_found.append((fa, rn, rd))

print(f'  Found {len(strb5_found)} STRB Rd, [Rn, #5] instructions')
for fa, rn, rd in strb5_found:
    # Show with context using disasm from that address
    off2 = fa2off(fa - 0x20)  # 16 instructions back
    code = fw[max(FILE_HEADER, off2):off2+60]
    start = (fa - 0x20) if off2 >= FILE_HEADER else FLASH_BASE
    print(f'\n  STRB r{rd}, [r{rn}, #5] @ {fa:#010x}:')
    ctx_code = fw[fa2off(fa-0x28):fa2off(fa)+0x10]
    ctx_fa = fa - 0x28
    for ins in md.disasm(ctx_code, ctx_fa):
        val = resolve_ldr_pc(ins)
        ann = f'  ; = 0x{val:08x}' if val else ''
        mark = ' ← HERE' if ins.address == fa else ''
        print(f'    {ins.address:#010x}: {ins.mnemonic} {ins.op_str}{ann}{mark}')

# fn_66f8 (tail-called from fn_bd04 for USB RESET)
disasm_fn(0x0800_66f8, 300, 'fn_66f8 (USB reset init?)', stop_early=False)

# Look at what's near CTR handler - the HID OUT handler might be nearby
disasm_fn(0x08005bd8, 300, 'CTR_handler @ 5bd8', stop_early=False)

# Also look at fn_ccd0 USB EP state init
disasm_fn(0x0800ccd0, 200, 'USB_ep_state_init @ ccd0', stop_early=False)

