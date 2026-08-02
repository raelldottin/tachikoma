#!/usr/bin/env python3
"""LLDB Python script to iterate all fields in the Configuration class."""

import lldb
import struct


def _call(debugger, frame, expr):
    eval_result = frame.EvaluateExpression(expr)
    if eval_result.GetError().Fail():
        return None
    return eval_result.GetValueAsUnsigned()


def _read_cstring(process, ptr):
    if ptr == 0:
        return None
    error = lldb.SBError()
    raw = process.ReadMemory(ptr, 256, error)
    if error.Fail():
        return None
    return raw.split(b'\x00')[0].decode('utf-8', errors='ignore')


def _read_utf16_string(process, string_ptr):
    if string_ptr == 0:
        return None

    error = lldb.SBError()
    length_data = process.ReadMemory(string_ptr + 0x10, 4, error)
    if error.Fail():
        return None

    length = struct.unpack('<i', length_data)[0]
    if length <= 0 or length > 4096:
        return None

    char_data = process.ReadMemory(string_ptr + 0x14, length * 2, error)
    if error.Fail():
        return None

    return char_data.decode('utf-16-le')


def enumerate_config_fields(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    process = target.GetProcess()
    thread = process.GetSelectedThread()
    frame = thread.GetSelectedFrame()

    base_addr = 0x11e1d4000

    funcs = {
        'il2cpp_class_get_fields': base_addr + 0x586f90,
        'il2cpp_field_get_name': base_addr + 0x58780c,
        'il2cpp_field_get_type': base_addr + 0x587830,
        'il2cpp_field_get_offset': base_addr + 0x58782c,
        'il2cpp_class_get_static_field_data': base_addr + 0x58706c,
        'il2cpp_string_chars': base_addr + 0x587ac4,
    }

    klass = 0x130f79290
    result.PutCString(f"Configuration class = 0x{klass:x}")

    static_data = _call(debugger, frame,
        f'((void*(*)(void*)){funcs["il2cpp_class_get_static_field_data"]})((void*){klass})')
    if static_data is None or static_data == 0:
        result.PutCString("ERROR: static_field_data is null")
        return
    result.PutCString(f"static_field_data = 0x{static_data:x}")

    # Allocate iterator storage in the target process
    iter_ptr = _call(debugger, frame, '(void*)calloc(1, 8)')
    if iter_ptr is None or iter_ptr == 0:
        result.PutCString("ERROR: failed to allocate iterator")
        return
    result.PutCString(f"Iterator at 0x{iter_ptr:x}")

    # Iterate fields
    result.PutCString("Fields:")
    for i in range(200):
        # Get next field - the iterator is passed by reference and modified
        field_ptr = _call(debugger, frame,
            f'((void*(*)(void*, void**)){funcs["il2cpp_class_get_fields"]})((void*){klass}, (void**){iter_ptr})')
        if field_ptr is None or field_ptr == 0:
            result.PutCString(f"  End of fields (iteration {i})")
            break

        # Read back the updated iterator from memory
        error = lldb.SBError()
        iter_data = process.ReadMemory(iter_ptr, 8, error)
        if not error.Fail():
            iter_val = struct.unpack('<Q', iter_data)[0]

        # Get field name
        name_ptr = _call(debugger, frame,
            f'((const char*(*)(void*)){funcs["il2cpp_field_get_name"]})((void*){field_ptr})')
        if name_ptr is None or name_ptr == 0:
            result.PutCString(f"  Field {i}: <no name>")
            continue

        field_name = _read_cstring(process, name_ptr)

        # Get field offset
        offset = _call(debugger, frame,
            f'((int(*)(void*)){funcs["il2cpp_field_get_offset"]})((void*){field_ptr})')

        result.PutCString(f"  Field {i}: {field_name} (ptr=0x{field_ptr:x}, offset={offset})")

        # If this looks like ChecksumKey or SavyChecksum, try to read its value
        if offset is not None and offset >= 0:
            ptr_addr = static_data + offset
            error = lldb.SBError()
            ptr_data = process.ReadMemory(ptr_addr, 8, error)
            if not error.Fail():
                string_ptr = struct.unpack('<Q', ptr_data)[0]
                if string_ptr != 0:
                    value = _read_utf16_string(process, string_ptr)
                    if value is not None:
                        result.PutCString(f"    VALUE: {value}")
                        # Flag if it's one of our targets
                        fn_lower = field_name.lower() if field_name else ""
                        if 'checksum' in fn_lower or 'savy' in fn_lower or 'key' in fn_lower:
                            result.PutCString(f"    *** TARGET FIELD ***")
                    else:
                        result.PutCString(f"    [string at 0x{string_ptr:x} but could not decode]")

    # Free iterator
    _call(debugger, frame, f'(void)free((void*){iter_ptr})')

    result.PutCString("DONE")


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand('command script add -f lldb_enum_fields.enumerate_config_fields enumerate_config_fields')