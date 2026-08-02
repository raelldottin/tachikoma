#!/usr/bin/env python3
"""
LLDB script to extract Configuration.ChecksumKey and Configuration.SavyChecksum
from a running Pixel Starships process.

Usage:
  lldb -n "Pixel Starships" -b -s scripts/extract_runtime_constants.py

Or attach to existing process:
  lldb -p <PID> -b -s scripts/extract_runtime_constants.py

This works around Frida anti-attach by using native LLDB on macOS.
"""

import lldb

def extract_constants(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    if not target:
        print("No target selected")
        return
    
    process = target.GetProcess()
    if not process:
        print("No process")
        return
    
    # Get the GameAssembly module
    module = None
    for i in range(target.GetNumModules()):
        mod = target.GetModuleAtIndex(i)
        if "GameAssembly" in mod.GetFileSpec().GetFilename():
            module = mod
            break
    
    if not module:
        print("GameAssembly module not found")
        return
    
    print(f"Found module: {module.GetFileSpec().GetFilename()}")
    print(f"Base address: 0x{module.GetObjectFileHeaderAddress().GetLoadAddress(target):x}")
    
    base = module.GetObjectFileHeaderAddress().GetLoadAddress(target)
    
    # RVAs from ISIL static analysis
    savy_rva = 0x4F7AAB0
    key_rva = 0x4F64F20
    
    savy_addr = base + savy_rva
    key_addr = base + key_rva
    
    print(f"\nSavyChecksum at: 0x{savy_addr:x}")
    print(f"ChecksumKey at: 0x{key_addr:x}")
    
    # Read pointer at those addresses (static field holds a pointer to the string)
    error = lldb.SBError()
    
    # Read 8 bytes (pointer on 64-bit)
    savy_ptr_data = process.ReadMemory(savy_addr, 8, error)
    if error.Success():
        savy_ptr = int.from_bytes(savy_ptr_data, 'little')
        print(f"SavyChecksum pointer: 0x{savy_ptr:x}")
        if savy_ptr != 0:
            # Read the string
            string_data = process.ReadCStringFromMemory(savy_ptr, 256, error)
            if error.Success():
                print(f"[+] SavyChecksum: {string_data}")
            else:
                print(f"[-] Failed to read string at 0x{savy_ptr:x}: {error}")
    else:
        print(f"[-] Failed to read pointer at 0x{savy_addr:x}: {error}")
    
    key_ptr_data = process.ReadMemory(key_addr, 8, error)
    if error.Success():
        key_ptr = int.from_bytes(key_ptr_data, 'little')
        print(f"ChecksumKey pointer: 0x{key_ptr:x}")
        if key_ptr != 0:
            string_data = process.ReadCStringFromMemory(key_ptr, 256, error)
            if error.Success():
                print(f"[+] ChecksumKey: {string_data}")
            else:
                print(f"[-] Failed to read string at 0x{key_ptr:x}: {error}")
    else:
        print(f"[-] Failed to read pointer at 0x{key_addr:x}: {error}")
    
    # Also try to find the .cctor and set a breakpoint
    print("\n=== Searching for Configuration..cctor ===")
    for i in range(module.GetNumCompileUnits()):
        cu = module.GetCompileUnitAtIndex(i)
        # Could enumerate functions here but IL2CPP symbols are stripped
    
    # Alternative: search for Configuration class in runtime
    print("\n=== Searching for Configuration type in IL2CPP metadata ===")
    # The il2cpp section contains type information
    # This would require parsing the IL2CPP metadata structures

def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand('command script add -f extract_runtime_constants.extract_constants extract_config')
    print("Loaded extract_config command. Run: (lldb) extract_config")

if __name__ == "__main__":
    # For standalone testing
    pass