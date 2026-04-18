#!/usr/bin/env python3
"""
Find the data section copy in fn_b150 and all EP table candidates.
Also look at Reset_Handler more carefully, and search for what writes to 0x20000074.
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

def read8(fa):
    off = fa2off(fa)
    if 0 <= off < len(fw):
        return fw[off]
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

# fn_b150: likely contains data+bss init
disasm_fn(0x0800b150, 200, 'fn_b150 (data/bss init)', stop_early=False)

# Reset_Handler: let's see the full startup sequence
disasm_fn(0x08005100, 100, 'Reset_Handler @ 5100', stop_early=False)

# Look for the USB Reset handler - this is where EP tables are initialized
# When USB_RESET interrupt fires, the firmware re-enumerates and sets up EP handler tables
# In the USB CTR handler we saw: the USB main body handles RESET
# Let's look at the Reset handling code
disasm_fn(0x0800cd18, 200, 'USB_main_body @ cd18', stop_early=False)

# All 50 EP table candidates
print('\n─── ALL 50 EP table candidates ───')
THUMB_MIN = 0x08005001
THUMB_MAX = 0x08016001

candidates = []
for off in range(FILE_HEADER, len(fw)-32, 4):
    v0 = struct.unpack_from('<I', fw, off)[0]
    if THUMB_MIN <= v0 <= THUMB_MAX and (v0 & 1):
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

for fa, seq in candidates:
    addrs = ' '.join(f'0x{v:08x}' for v in seq)
    print(f'  flash {fa:#010x}: {addrs}')

# More specifically: search for sequences without zeros (more likely to be EP tables)
print('\n─── EP candidates without zeros (pure function pointer arrays) ───')
for off in range(FILE_HEADER, len(fw)-32, 4):
    v0 = struct.unpack_from('<I', fw, off)[0]
    if THUMB_MIN <= v0 <= THUMB_MAX and (v0 & 1):
        seq = [v0]
        for j in range(1, 8):
            vj = struct.unpack_from('<I', fw, off + j*4)[0]
            if THUMB_MIN <= vj <= THUMB_MAX and (vj & 1):
                seq.append(vj)
            else:
                break
        if len(seq) >= 4:
            fa = off - FILE_HEADER + FLASH_BASE
            addrs = ' '.join(f'0x{v:08x}' for v in seq)
            print(f'  flash {fa:#010x}: {addrs}')

# Now search for all LDR that load values and then STR to 0x20000074 or 0x20000090
print('\n─── Search for STR instructions near 0x20000074/0x20000090 addresses ───')
all_code = fw[FILE_HEADER:]
all_insns = list(md.disasm(all_code, FLASH_BASE))

EP_RX_TABLE = 0x20000074
EP_TX_TABLE = 0x20000090

for i, ins in enumerate(all_insns):
    if ins.mnemonic in ('str', 'str.w', 'strh', 'strb', 'stm', 'stm.w'):
        pass
    # Look for ldr of EP table addresses
    if ins.mnemonic in ('ldr', 'ldr.w'):
        val = resolve_ldr_pc(ins)
        if val in (EP_RX_TABLE, EP_TX_TABLE, 0x20000074, 0x20000090):
            reg = ins.op_str.split(',')[0].strip()
            label = 'RX_table' if val == EP_RX_TABLE else 'TX_table'
            print(f'\n  LDR {label}({val:#x}) at {ins.address:#010x}, reg={reg}')
            for j in range(max(0,i-5), min(len(all_insns), i+15)):
                ni = all_insns[j]
                v2 = resolve_ldr_pc(ni)
                vann = f'  ; = 0x{v2:08x}' if v2 else ''
                mark = ' ←' if j==i else ''
                print(f'    {ni.address:#010x}: {ni.mnemonic} {ni.op_str}{vann}{mark}')
