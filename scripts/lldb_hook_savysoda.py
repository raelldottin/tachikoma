#!/usr/bin/env python3
"""LLDB Python script: breakpoint on SavysodaEncryptString, read preimage string."""
import lldb

def handle_breakpoint(frame, bp_loc, internal_dict):
    """Called when SavysodaEncryptString breakpoint hits."""
    # x0 = Il2CppString* preimage (the string that will be encrypted)
    thread = frame.GetThread()
    process = thread.GetProcess()
    target = process.GetTarget()
    
    # Read x0 register
    x0_val = frame.FindRegister("x0").GetValue()
    if x0_val is None:
        print("ERROR: Could not read x0")
        return False  # auto-continue
    
    x0_addr = int(x0_val, 16) if x0_val.startswith("0x") else int(x0_val)
    
    # Read Il2CppString structure:
    # +0x00: class pointer
    # +0x08: monitor 
    # +0x10: length (int32)
    # +0x14: chars (UTF-16LE)
    
    # Read length at offset 0x10
    error = lldb.SBError()
    length_bytes = process.ReadMemory(x0_addr + 0x10, 4, error)
    if not error.Success():
        print(f"ERROR reading length: {error.GetDescription()}")
        return False
    
    length = int.from_bytes(length_bytes, 'little')
    print(f"PREIMAGE length={length}")
    
    # Read UTF-16LE chars at offset 0x14
    char_bytes = process.ReadMemory(x0_addr + 0x14, length * 2, error)
    if not error.Success():
        print(f"ERROR reading chars: {error.GetDescription()}")
        return False
    
    preimage = char_bytes.decode('utf-16-le')
    print(f"PREIMAGE={preimage}")
    
    # Write to file
    with open('/tmp/savysoda_preimage.txt', 'a') as f:
        f.write(preimage + '\n')
    
    # Don't auto-continue - stop here so we can see it
    return False  # False = auto-continue

def __lldb_init_module(debugger, internal_dict):
    """Initialize when module is loaded."""
    target = debugger.GetSelectedTarget()
    if not target:
        print("No target")
        return
    
    process = target.GetProcess()
    if not process:
        print("No process")
        return
    
    # Set breakpoint on SavysodaEncryptString
    bp = target.BreakpointCreateByAddress(0x11ecc6f10)
    bp.SetScriptCallbackFunction("lldb_hook_savysoda.handle_breakpoint")
    print(f"Breakpoint set: {bp.GetID()} at SavysodaEncryptString")
    print("Continue the process to trigger the breakpoint...")
