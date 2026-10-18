"""Minimal PE import-table reader.

Given a .pyd/.dll/.exe, return the list of DLL names it imports. Used to pull
in the non-system DLLs that an extension module needs (e.g. _ssl.pyd ->
libcrypto-3.dll). Pure stdlib, no dependencies.
"""

from __future__ import annotations

import struct


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def imported_dlls(path: str) -> list[str]:
    """Return lowercase DLL names in the import directory of a PE file."""
    with open(path, "rb") as f:
        data = f.read()

    if data[:2] != b"MZ":
        return []
    e_lfanew = _u32(data, 0x3C)
    if data[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        return []

    coff = e_lfanew + 4
    num_sections = _u16(data, coff + 2)
    opt_size = _u16(data, coff + 16)
    opt = coff + 20
    magic = _u16(data, opt)
    if magic == 0x20B:          # PE32+ (64-bit)
        dd_off = opt + 112
    elif magic == 0x10B:        # PE32 (32-bit)
        dd_off = opt + 96
    else:
        return []

    # Data directory entry 1 = import table (RVA, size)
    import_rva = _u32(data, dd_off + 1 * 8)
    if import_rva == 0:
        return []

    # Section headers follow the optional header.
    sect = opt + opt_size
    sections = []
    for i in range(num_sections):
        base = sect + i * 40
        vaddr = _u32(data, base + 12)
        vsize = _u32(data, base + 8)
        raw_ptr = _u32(data, base + 20)
        raw_size = _u32(data, base + 16)
        sections.append((vaddr, max(vsize, raw_size), raw_ptr))

    def rva_to_off(rva):
        for vaddr, vsize, raw_ptr in sections:
            if vaddr <= rva < vaddr + vsize:
                return raw_ptr + (rva - vaddr)
        return None

    names = []
    off = rva_to_off(import_rva)
    if off is None:
        return []
    # Array of IMAGE_IMPORT_DESCRIPTOR (20 bytes), terminated by all-zero entry.
    while True:
        name_rva = _u32(data, off + 12)
        if name_rva == 0 and _u32(data, off) == 0:
            break
        noff = rva_to_off(name_rva)
        if noff is not None:
            end = data.find(b"\0", noff)
            try:
                names.append(data[noff:end].decode("ascii").lower())
            except UnicodeDecodeError:
                pass
        off += 20
        if off + 20 > len(data):
            break
    return names
