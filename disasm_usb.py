#!/usr/bin/env python3
"""
Disassemble the USB receive handler in DSP-408-Firmware.bin.

Firmware layout:
  File offset 0..7  : WMCU header (8 bytes)
  File offset 8..   : Application image, loaded at flash 0x08005000
  => file_offset = (flash_addr - 0x08005000) + 8

USB_LP_CAN_RX0 handler: flash 0x0800cdd4
"""
import struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

FIRMWARE = '/Users/mbelleau/Code/usb_dsp_mac/downloads/DSP-408-Firmware.bin'
FLASH_BASE = 0x08005000
FILE_HEADER = 8

def fa2off(addr):
    return (addr - FLASH_BASE) + FILE_HEADER

def off2fa(off):
    return (off - FILE_HEADER) + FLASH_BASE

with open(FIRMWARE, 'rb') as f:
    fw = f.read()

print(f'Firmware size: {len(fw):#x} bytes')

# The handler is at flash 0x0800cdd4 — that's a Thumb address so bit0 may be set in vectors,
# but the actual code is at the even address.
USB_HANDLER_FA = 0x0800cdd4
# If the vector stored (addr|1), strip bit0
USB_HANDLER_FA &= ~1

off = fa2off(USB_HANDLER_FA)
print(f'USB handler: flash={USB_HANDLER_FA:#010x}  file_offset={off:#x}')
print()

# Disassemble 512 bytes (enough to see the full handler)
code = fw[off:off+512]

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
md.detail = True

instructions = list(md.disasm(code, USB_HANDLER_FA))

print(f'Disassembled {len(instructions)} instructions:')
print()

# Print with addresses
for i, ins in enumerate(instructions):
    print(f'  {ins.address:#010x}:  {ins.bytes.hex():<12s}  {ins.mnemonic:<8s} {ins.op_str}')
    # Stop at second BX LR or POP {pc} — likely end of function
    if i > 0 and ins.mnemonic in ('bx', 'pop') and ('pc' in ins.op_str or 'lr' in ins.op_str):
        # count returns seen
        pass

print()
print('─── Looking for key patterns ───')

# Find CMP instructions (command byte checks)
for ins in instructions:
    if ins.mnemonic == 'cmp':
        print(f'  CMP  {ins.address:#010x}: {ins.op_str}')
    elif ins.mnemonic in ('ldrb', 'ldr') and '[' in ins.op_str:
        print(f'  LOAD {ins.address:#010x}: {ins.mnemonic} {ins.op_str}')

print()

# Also let's look at what addresses are referenced (LDR PC-relative loads)
print('─── PC-relative loads (data refs) ───')
for ins in instructions:
    if ins.mnemonic == 'ldr' and '[pc' in ins.op_str.lower():
        # The value is at (ins.address + 4 + imm) & ~3
        ops = ins.op_str  # e.g. "r0, [pc, #0x10]"
        try:
            imm_str = ops.split('#')[1].rstrip(']')
            imm = int(imm_str, 0)
            ref_addr = ((ins.address + 4) & ~3) + imm
            ref_off = fa2off(ref_addr)
            if 0 <= ref_off < len(fw) - 3:
                val = struct.unpack_from('<I', fw, ref_off)[0]
                print(f'  {ins.address:#010x}: ldr {ops.split(",")[0].strip()}, =0x{val:08x}  (from {ref_addr:#010x})')
            else:
                print(f'  {ins.address:#010x}: ldr {ops} → ref {ref_addr:#010x} OOB')
        except Exception as e:
            print(f'  {ins.address:#010x}: ldr {ops} → parse error: {e}')
