#!/usr/bin/env python3
"""
1. Disassemble C startup (0x0800bd6c) to find data section copy → SRAM function pointer tables
2. Read what's initialized at SRAM 0x20000074 and 0x20000090 (EP handler tables)
3. Look at 0x0800aa80 (USART1 cmd handler) to understand USB HID protocol
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

# C startup
disasm_fn(0x0800bd6c, 300, 'C_startup @ bd6c', stop_early=False)

# USART1 command handler (tail-called from USART1_IRQHandler)
disasm_fn(0x0800aa80, 400, 'USART1_cmd_handler @ aa80')

# Look for the EP handler init in the flash data section
# The C startup copies from [flash_data_start .. flash_data_end] → SRAM_base
# The tables at 0x20000074 and 0x20000090 should have initial values in flash
# Scan the firmware for patterns of Thumb function pointers (LSB=1, in flash range)
# stored at predictable offsets that would map to SRAM 0x20000074

# The data section in flash typically follows the code.
# Total firmware size = 0x11298 bytes. Code likely takes most of it.
# Let's search for sequences of 4-byte aligned Thumb pointers in the 0x08005xxx range

print('\n─── Searching for EP handler table initial values in flash data section ───')
# Search for the sequence: [some flash addr | 1, another flash addr | 1, ...]
# at any 4-byte aligned position in the firmware
THUMB_MIN = 0x08005001
THUMB_MAX = 0x08016001

candidates = []
for off in range(FILE_HEADER, len(fw)-32, 4):
    v0 = struct.unpack_from('<I', fw, off)[0]
    if THUMB_MIN <= v0 <= THUMB_MAX and (v0 & 1):
        # Check next few words
        seq = [v0]
        for j in range(1, 8):
            vj = struct.unpack_from('<I', fw, off + j*4)[0]
            if THUMB_MIN <= vj <= THUMB_MAX and (vj & 1):
                seq.append(vj)
            elif vj == 0:
                seq.append(0)
            else:
                break
        if len(seq) >= 4:
            fa = off - FILE_HEADER + FLASH_BASE
            candidates.append((fa, seq))

print(f'  Found {len(candidates)} potential function pointer arrays')
print('  First 20:')
for fa, seq in candidates[:20]:
    addrs = ' '.join(f'0x{v:08x}' for v in seq)
    print(f'    flash {fa:#010x}: {addrs}')

# The ep handler tables in SRAM:
# 0x20000074: RX handlers for EP1..EP7
# 0x20000090: TX handlers for EP1..EP7
# Each entry is a Thumb function pointer. Let's check if any of the
# flash data sequences map to these SRAM addresses.

# First, let's look at the C startup to find data section boundaries
print('\n─── C startup analysis for data section boundaries ───')
off = fa2off(0x0800bd6c)
code = fw[off:off+200]
for ins in md.disasm(code, 0x0800bd6c):
    ann = ''
    val = resolve_ldr_pc(ins)
    if val is not None:
        ann = f'  ; = 0x{val:08x}'
    if val and (0x20000000 <= val <= 0x20010000):
        ann += ' ← SRAM'
    print(f'  {ins.address:#010x}:  {ins.bytes.hex():<12s}  {ins.mnemonic:<8s} {ins.op_str}{ann}')
    if ins.mnemonic == 'bx' and 'lr' in ins.op_str:
        break
