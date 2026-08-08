#!/usr/bin/env python3
"""
Extract Configuration.ChecksumKey and Configuration.SavyChecksum from GameAssembly.dylib
at known RVAs from Cpp2IL static analysis.

RVAs (from ISIL):
- SavyChecksum: 0x4F7AAB0
- ChecksumKey: 0x4F64F20

These are static string fields in the Configuration class. In IL2CPP, static fields
are stored in the .data section. The RVA is the relative virtual address within the
binary's memory layout.
"""

import sys
import os

def read_cstring_at_offset(data, offset):
    """Read a null-terminated UTF-8 string at the given offset."""
    end = data.find(b'\x00', offset)
    if end == -1:
        end = offset + 256  # safety limit
    return data[offset:end].decode('utf-8', errors='ignore')

def extract_strings_from_rva(dylib_path, rva, base_addr=0x100000000):
    """Extract string at RVA from Mach-O binary.
    
    Note: The base address for Mach-O arm64 is typically 0x100000000.
    The file offset = RVA - (virtual_address_of_section - file_offset_of_section)
    """
    with open(dylib_path, 'rb') as f:
        data = f.read()
    
    # Try direct RVA as file offset (works if binary is not rebased)
    if rva < len(data):
        s = read_cstring_at_offset(data, rva)
        if s and len(s) > 5:
            print(f"RVA 0x{rva:X} (direct): '{s[:100]}'")
            return s
    
    # Try with common Mach-O base
    file_offset = rva - 0x100000000
    if 0 <= file_offset < len(data):
        s = read_cstring_at_offset(data, file_offset)
        if s and len(s) > 5:
            print(f"RVA 0x{rva:X} (based): '{s[:100]}'")
            return s
    
    # Search nearby for plausible strings
    search_start = max(0, rva - 1024)
    search_end = min(len(data), rva + 1024)
    for i in range(search_start, search_end):
        if data[i:i+1].isalnum() or data[i] in b'-_':
            s = read_cstring_at_offset(data, i)
            if s and len(s) >= 16 and all(c in '0123456789abcdefABCDEF' for c in s):
                print(f"RVA 0x{rva:X} (nearby 0x{i:X}): '{s[:64]}'")
    
    return None


def main():
    # Default path on macOS
    dylib_path = "/Applications/Pixel Starships.app/Contents/Frameworks/GameAssembly.dylib"
    
    if len(sys.argv) > 1:
        dylib_path = sys.argv[1]
    
    if not os.path.exists(dylib_path):
        print(f"ERROR: {dylib_path} not found")
        sys.exit(1)
    
    print(f"Reading: {dylib_path}")
    print(f"Size: {os.path.getsize(dylib_path)} bytes")
    
    print("\n=== Extracting SavyChecksum (RVA 0x4F7AAB0) ===")
    savy = extract_strings_from_rva(dylib_path, 0x4F7AAB0)
    
    print("\n=== Extracting ChecksumKey (RVA 0x4F64F20) ===")
    key = extract_strings_from_rva(dylib_path, 0x4F64F20)
    
    print("\n=== Results ===")
    print(f"SavyChecksum: {savy or 'NOT FOUND'}")
    print(f"ChecksumKey: {key or 'NOT FOUND'}")


if __name__ == "__main__":
    main()