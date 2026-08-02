#!/usr/bin/env python3
"""
Extract Configuration.ChecksumKey and Configuration.SavyChecksum from GameAssembly.dylib
using lief to properly parse Mach-O sections.
"""

import sys
import os

try:
    import lief
except ImportError:
    print("Installing lief...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "lief"])
    import lief

def extract_string_at_rva(dylib_path, rva):
    """Extract a null-terminated UTF-8 string at the given RVA using lief section mapping."""
    binary = lief.parse(dylib_path)
    if not binary:
        print("Failed to parse binary")
        return None
    
    print(f"Binary: {binary.header.cpu_type} / {binary.header.file_type}")
    print(f"Entry point: 0x{binary.entrypoint:X}")
    
    # Find the section containing this RVA
    for section in binary.sections:
        vaddr = section.virtual_address
        vsize = section.size
        if vaddr <= rva < vaddr + vsize:
            offset_in_section = rva - vaddr
            file_offset = section.offset + offset_in_section
            
            print(f"Found in section: {section.name}")
            print(f"  Section VA: 0x{vaddr:X}, Size: 0x{vsize:X}")
            print(f"  Section offset: 0x{section.offset:X}")
            print(f"  RVA 0x{rva:X} -> file offset 0x{file_offset:X}")
            
            # Read the string from the section content
            content = section.content
            if offset_in_section < len(content):
                # Find null terminator
                end = offset_in_section
                while end < len(content) and content[end] != 0:
                    end += 1
                string_bytes = bytes(content[offset_in_section:end])
                try:
                    return string_bytes.decode('utf-8')
                except UnicodeDecodeError:
                    return None
            break
    else:
        print(f"RVA 0x{rva:X} not found in any section")
        # Print all sections for debugging
        for section in binary.sections:
            print(f"  {section.name}: VA=0x{section.virtual_address:X}, Size=0x{section.size:X}, Offset=0x{section.offset:X}")
    
    return None

def search_for_hex_strings(dylib_path, rva, window=4096):
    """Search for 32-char hex strings near the RVA."""
    binary = lief.parse(dylib_path)
    if not binary:
        return []
    
    for section in binary.sections:
        vaddr = section.virtual_address
        vsize = section.size
        search_start = max(vaddr, rva - window)
        search_end = min(vaddr + vsize, rva + window)
        
        if search_start < search_end:
            offset_start = search_start - vaddr
            offset_end = search_end - vaddr
            content = bytes(section.content[offset_start:offset_end])
            
            # Search for 32-char hex strings
            i = 0
            results = []
            while i < len(content):
                if content[i:i+1].isalnum() or content[i] in b'-_':
                    end = i
                    while end < len(content) and (content[end:end+1].isalnum() or content[end] in b'-_'):
                        end += 1
                    candidate = content[i:end]
                    if len(candidate) >= 32:
                        try:
                            s = candidate.decode('utf-8')
                            if all(c in '0123456789abcdefABCDEF' for c in s):
                                results.append((search_start + i, s))
                        except UnicodeDecodeError:
                            pass
                    i = end
                else:
                    i += 1
            
            if results:
                print(f"\nSection {section.name} near RVA 0x{rva:X}:")
                for addr, s in results[:10]:
                    print(f"  0x{addr:X}: {s[:64]}")
    
    return []

def main():
    dylib_path = "/Applications/Pixel Starships.app/Contents/Frameworks/GameAssembly.dylib"
    
    if len(sys.argv) > 1:
        dylib_path = sys.argv[1]
    
    if not os.path.exists(dylib_path):
        print(f"ERROR: {dylib_path} not found")
        sys.exit(1)
    
    print(f"Reading: {dylib_path}")
    print(f"Size: {os.path.getsize(dylib_path)} bytes")
    
    print("\n=== Extracting SavyChecksum (RVA 0x4F7AAB0) ===")
    savy = extract_string_at_rva(dylib_path, 0x4F7AAB0)
    search_for_hex_strings(dylib_path, 0x4F7AAB0)
    
    print("\n=== Extracting ChecksumKey (RVA 0x4F64F20) ===")
    key = extract_string_at_rva(dylib_path, 0x4F64F20)
    search_for_hex_strings(dylib_path, 0x4F64F20)
    
    print("\n=== Results ===")
    print(f"SavyChecksum: {savy or 'NOT FOUND'}")
    print(f"ChecksumKey: {key or 'NOT FOUND'}")


if __name__ == "__main__":
    main()