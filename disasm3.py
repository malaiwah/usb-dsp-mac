#!/usr/bin/env python3
"""
Disassemble the CTR (Correct Transfer) handler at 0x08005bd8
This is called when a USB HID packet is received.
Also follows what it calls.
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

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
md.detail = True

def resolve_ldr_pc(ins):
    """Resolve [pc, #imm] to the 32-bit word it loads."""
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

def disasm_fn(start_fa, max_bytes=512, label=''):
    off = fa2off(start_fa)
    code = fw[off:off+max_bytes]
    print(f'\n{"═"*70}')
    print(f'  {label}  @  {start_fa:#010x}  (file +{off:#x})')
    print(f'{"═"*70}')
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

# CTR handler — called when USB correctly receives (or transmits) a packet
disasm_fn(0x0800_5bd8, 512, 'USB CTR (Correct Transfer) handler')

# Also the handler at 0x0800ab8c (called for RESET/SUSPEND/RESUME events)
disasm_fn(0x0800_ab8c, 256, 'USB_reset_or_suspend_handler')

# The "report received" function referenced at 0x200000d4+4 (function pointer)
# Let's find what function pointers are stored there
print('\n─── Checking function pointer table at 0x200000d4 ───')
off = fa2off(0x200000d4) if fa2off(0x200000d4) >= 0 else None
# 0x200000d4 is SRAM — not in the firmware binary. Let's look for references to this address.
# Actually the vector is loaded at runtime — search flash for who writes to this address.
# Let's look for the initialization code (Reset_Handler → main → USB_init)

# Better: scan firmware for the literal 0x200000d4 (as a 32-bit constant)
target = struct.pack('<I', 0x200000d4)
matches = []
for i in range(0, len(fw)-4, 2):
    if fw[i:i+4] == target:
        matches.append(i)
print(f'  References to 0x200000d4 in flash: {[hex(fa2off.__wrapped__(m) if hasattr(fa2off,"__wrapped__") else (m + FLASH_BASE - FILE_HEADER)) for m in matches]}')
# Re-derive: file_offset m → flash addr = m - FILE_HEADER + FLASH_BASE
for m in matches:
    fa = m - FILE_HEADER + FLASH_BASE
    print(f'    file_offset={m:#x} → flash={fa:#010x}')

# The actual USB receive processing — after CTR fires and data is in PMA,
# the firmware copies from PMA to SRAM and then dispatches on command byte.
# Let's find the main command dispatch by searching for comparison sequences.
# Hypothesis: command byte is at offset 0 of the received packet.
# Search for: ldrb rN, [rM] followed by cmp rN, #<cmd>

print('\n─── Searching for CMP #<byte> chains (command dispatch) ───')
code_start = fa2off(0x08005000)
code_end   = fa2off(0x08010000) if fa2off(0x08010000) < len(fw) else len(fw)
chunk = fw[code_start:code_end]

instructions = list(md.disasm(chunk, 0x08005000))
for i, ins in enumerate(instructions):
    if ins.mnemonic == 'cmp' and '#' in ins.op_str:
        try:
            val = int(ins.op_str.split('#')[1], 0)
            # Look for chains of CMP with consecutive or small values
            # Count how many CMP instructions follow within 20 insns
            cmp_vals = [val]
            for j in range(1, 20):
                if i+j >= len(instructions): break
                ni = instructions[i+j]
                if ni.mnemonic == 'cmp' and '#' in ni.op_str:
                    cmp_vals.append(int(ni.op_str.split('#')[1], 0))
                elif ni.mnemonic not in ('beq','bne','bhs','blo','bls','bhi','bge','blt','ble','bgt','b','bl','blx','mov','movs','movw','movt','ldr','ldrb','str','strb','push','pop','nop','it'):
                    break
            if len(cmp_vals) >= 3:
                print(f'  {ins.address:#010x}: CMP chain {cmp_vals}')
        except Exception:
            pass
