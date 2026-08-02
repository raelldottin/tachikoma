#!/usr/bin/env python3
"""LLDB Python script to extract Configuration.ChecksumKey and Configuration.SavyChecksum
from a known Configuration class address."""

import lldb
import struct


def _call(debugger, frame, expr):
    """Evaluate an expression and return the unsigned value, or None on error."""
    eval_result = frame.EvaluateExpression(expr)
    if eval_result.GetError().Fail():
        stream = lldb.SBStream()
        eval_result.GetError().GetDescription(stream)
        return None
    return eval_result.GetValueAsUnsigned()


def _read_utf16_string(process, string_ptr):
    """Read an Il2CppString object and decode its UTF-16 content."""
    if string_ptr == 0:
        return None

    error = lldb.SBError()

    # Read length at offset 0x10
    length_data = process.ReadMemory(string_ptr + 0x10, 4, error)
    if error.Fail():
        return None

    length = struct.unpack('<i', length_data)[0]
    if length < 0 or length > 4096:
        return None

    # Read UTF-16LE chars at offset 0x14
    char_data = process.ReadMemory(string_ptr + 0x14, length * 2, error)
    if error.Fail():
        return None

    return char_data.decode('utf-16-le')


def extract_checksum_constants(debugger, command, result, internal_dict):
    """Extract ChecksumKey and SavyChecksum from Configuration class."""
    target = debugger.GetSelectedTarget()
    process = target.GetProcess()
    thread = process.GetSelectedThread()
    frame = thread.GetSelectedFrame()

    base_addr = 0x11e1d4000

    funcs = {
        'il2cpp_class_get_field_from_name': base_addr + 0x586fa4,
        'il2cpp_field_get_offset': base_addr + 0x58782c,
        'il2cpp_class_get_static_field_data': base_addr + 0x58706c,
        'il2cpp_string_chars': base_addr + 0x587ac4,
    }

    # Configuration class address found by find_configuration_class
    klass = 0x130f79290
    result.PutCString(f"Configuration class = 0x{klass:x}")

    # Get static field data
    static_data = _call(debugger, frame,
        f'((void*(*)(void*)){funcs["il2cpp_class_get_static_field_data"]})((void*){klass})')
    if static_data is None or static_data == 0:
        result.PutCString("ERROR: static_field_data is null or failed")
        return
    result.PutCString(f"static_field_data = 0x{static_data:x}")

    # Helper: extract a string field by name
    def extract_field(field_name):
        field_ptr = _call(debugger, frame,
            f'((void*(*)(void*, const char*)){funcs["il2cpp_class_get_field_from_name"]})((void*){klass}, "{field_name}")')
        if field_ptr is None or field_ptr == 0:
            result.PutCString(f"ERROR: could not get {field_name} field")
            return None

        offset = _call(debugger, frame,
            f'((int(*)(void*)){funcs["il2cpp_field_get_offset"]})((void*){field_ptr})')
        if offset is None:
            result.PutCString(f"ERROR: could not get {field_name} offset")
            return None

        result.PutCString(f"{field_name}: field=0x{field_ptr:x}, offset={offset}")

        # For static fields, the offset is from the static_field_data base
        if offset < 0:
            result.PutCString(f"WARNING: {field_name} offset is negative: {offset}")
            return None

        ptr_addr = static_data + offset
        error = lldb.SBError()
        ptr_data = process.ReadMemory(ptr_addr, 8, error)
        if error.Fail():
            result.PutCString(f"ERROR: could not read memory at 0x{ptr_addr:x}: {error.GetCString()}")
            return None

        string_ptr = struct.unpack('<Q', ptr_data)[0]
        result.PutCString(f"{field_name}: Il2CppString ptr = 0x{string_ptr:x}")

        if string_ptr == 0:
            result.PutCString(f"WARNING: {field_name} string pointer is null (field not initialized)")
            return None

        # Try to decode the string directly from the Il2CppString object
        value = _read_utf16_string(process, string_ptr)
        if value is not None:
            return value

        # Fallback: use il2cpp_string_chars
        result.PutCString(f"Trying il2cpp_string_chars fallback for {field_name}")
        chars_ptr = _call(debugger, frame,
            f'((const wchar_t*(*)(void*)){funcs["il2cpp_string_chars"]})((void*){string_ptr})')
        if chars_ptr is not None and chars_ptr != 0:
            error = lldb.SBError()
            raw = process.ReadMemory(chars_ptr, 512, error)
            if not error.Fail():
                # Find double-null terminator (UTF-16)
                try:
                    decoded = raw.split(b'\x00\x00')[0].decode('utf-16-le')
                    if decoded:
                        return decoded
                except Exception:
                    pass
                result.PutCString(f"{field_name}: raw hex = {raw[:128].hex()}")

        result.PutCString(f"WARNING: Could not decode {field_name}")
        return None

    # Extract ChecksumKey
    ck_value = extract_field("ChecksumKey")
    if ck_value is not None:
        result.PutCString(f"RESULT: ChecksumKey = {ck_value}")
    else:
        result.PutCString("FAILED: ChecksumKey extraction")

    # Extract SavyChecksum
    savy_value = extract_field("SavyChecksum")
    if savy_value is not None:
        result.PutCString(f"RESULT: SavyChecksum = {savy_value}")
    else:
        result.PutCString("FAILED: SavyChecksum extraction")

    result.PutCString("DONE: extraction complete")


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand(
        'command script add -f lldb_extract_checksum.extract_checksum_constants extract_checksum_constants'
    )