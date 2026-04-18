#!/usr/bin/env python3
"""
Follow the call chain from the USB interrupt handler.
USB ISR at 0x0800cdd4 → b.w → 0x0800cd18
Also finds the HID RX buffer and command dispatch.
"""
import struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

FIRMWARE = '/Users/mbelleau/Code/usb_dsp_mac/downloads/DSP-408-Firmware.bin'
FLASH_BASE = 0x08005000
FILE_HEADER = 8
SRAM_BASE   = 0x20000000

with open(FIRMWARE, 'rb') as f:
    fw = f.read()

def fa2off(addr):
    return (addr - FLASH_BASE) + FILE_HEADER

def disasm(start_fa, nbytes=256, label=''):
    off = fa2off(start_fa)
    code = fw[off:off+nbytes]
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    print(f'\n{"─"*60}')
    print(f'Function: {label}  @ {start_fa:#010x}  (file off {off:#x})')
    print(f'{"─"*60}')
    for ins in md.disasm(code, start_fa):
        ann = ''
        # Annotate LDR [pc, #n] with the resolved value
        if ins.mnemonic == 'ldr' and '[pc' in ins.op_str.lower():
            try:
                imm_str = ins.op_str.split('#')[1].rstrip(']').strip()
                imm = int(imm_str, 0)
                ref_fa = ((ins.address + 4) & ~3) + imm
                ref_off = fa2off(ref_fa)
                if 0 <= ref_off < len(fw) - 3:
                    val = struct.unpack_from('<I', fw, ref_off)[0]
                    ann = f'  ; =0x{val:08x}'
            except Exception:
                pass
        print(f'  {ins.address:#010x}:  {ins.bytes.hex():<12s}  {ins.mnemonic:<8s} {ins.op_str}{ann}')
        # Stop at pop {pc} or unconditional return after we've seen a push
        if ins.mnemonic in ('bx',) and 'lr' in ins.op_str:
            break
        if ins.mnemonic == 'pop' and 'pc' in ins.op_str and ins.address > start_fa + 4:
            break

# The USB ISR jumps here:
disasm(0x0800cd18, 512, 'USB_LP_CAN_RX0 → main body')

# The function referenced at 0x0800b10c (called with r0=2)
disasm(0x0800b10c, 256, 'USB tx/enqueue? (called with r0=2)')

# 0x8006b92 and 0x8006b96 — called repeatedly (USB endpoint control?)
disasm(0x0800_6b92, 128, 'fn_6b92')
disasm(0x0800_6be4, 64, 'fn_6be4 (wait/poll)')

# 0x080063ae — delay/wait (called with r0=0x64=100, 0x1f4=500, 0xc8=200, 0xa=10)
disasm(0x080063ae, 64, 'delay_ms?')

# 0x0800c284 — called with r0=9 repeatedly (USB send?)
disasm(0x0800c284, 256, 'USB_send? (r0=9 endpoint?)')
