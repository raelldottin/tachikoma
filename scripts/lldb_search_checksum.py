#!/usr/bin/env python3
"""LLDB Python script to search all classes for fields containing 'checksum' or 'savy'."""

import lldb


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


def search_checksum_fields(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    process = target.GetProcess()
    thread = process.GetSelectedThread()
    frame = thread.GetSelectedFrame()

    base_addr = 0x11e1d4000

    funcs = {
        'il2cpp_class_get_fields': base_addr + 0x586f90,
        'il2cpp_field_get_name': base_addr + 0x58780c,
        'il2cpp_field_get_offset': base_addr + 0x58782c,
        'il2cpp_class_get_static_field_data': base_addr + 0x58706c,
        'il2cpp_image_get_class_count': base_addr + 0x587dac,
        'il2cpp_image_get_class': base_addr + 0x587dc4,
        'il2cpp_class_get_name': base_addr + 0x586fb0,
        'il2cpp_class_get_namespace': base_addr + 0x586fb4,
        'il2cpp_image_get_name': base_addr + 0x587da0,
    }

    domain_get = base_addr + 0x587584
    assembly_open = base_addr + 0x587588
    image_get = base_addr + 0x586f4c

    # Get domain
    domain = _call(debugger, frame, f"((void*(*)(void)){domain_get})()")
    if domain is None:
        result.PutCString("ERROR: Could not get domain")
        return

    # Open assemblies
    assemblies = []
    for asm_name in ["Assembly-CSharp", "Assembly-CSharp-firstpass"]:
        asm = _call(debugger, frame,
            f'((void*(*)(void*, const char*)){assembly_open})((void*){domain}, "{asm_name}")')
        if asm is not None and asm != 0:
            assemblies.append((asm_name, asm))

    for asm_name, asm_addr in assemblies:
        img = _call(debugger, frame, f'((void*(*)(void*)){image_get})((void*){asm_addr})')
        if img is None or img == 0:
            continue

        count = _call(debugger, frame,
            f'((int(*)(void*)){funcs["il2cpp_image_get_class_count"]})((void*){img})')
        if count is None:
            continue

        result.PutCString(f"Searching {count} classes in {asm_name}...")

        # Allocate iterator
        iter_ptr = _call(debugger, frame, '(void*)calloc(1, 8)')
        if iter_ptr is None or iter_ptr == 0:
            continue

        for i in range(count):
            klass = _call(debugger, frame,
                f'((void*(*)(void*, int)){funcs["il2cpp_image_get_class"]})((void*){img}, {i})')
            if klass is None or klass == 0:
                continue

            # Get class name
            name_ptr = _call(debugger, frame,
                f'((const char*(*)(void*)){funcs["il2cpp_class_get_name"]})((void*){klass})')
            if name_ptr is None or name_ptr == 0:
                continue
            class_name = _read_cstring(process, name_ptr)

            # Get namespace
            ns_ptr = _call(debugger, frame,
                f'((const char*(*)(void*)){funcs["il2cpp_class_get_namespace"]})((void*){klass})')
            ns = _read_cstring(process, ns_ptr) if ns_ptr else ""

            # Enumerate fields
            # Reset iterator
            error = lldb.SBError()
            process.WriteMemory(iter_ptr, b'\x00' * 8, error)

            field_count = 0
            for _ in range(200):
                field_ptr = _call(debugger, frame,
                    f'((void*(*)(void*, void**)){funcs["il2cpp_class_get_fields"]})((void*){klass}, (void**){iter_ptr})')
                if field_ptr is None or field_ptr == 0:
                    break

                fname_ptr = _call(debugger, frame,
                    f'((const char*(*)(void*)){funcs["il2cpp_field_get_name"]})((void*){field_ptr})')
                if fname_ptr is None or fname_ptr == 0:
                    continue
                field_name = _read_cstring(process, fname_ptr)

                fn_lower = field_name.lower() if field_name else ""
                if 'checksum' in fn_lower or 'savy' in fn_lower:
                    offset = _call(debugger, frame,
                        f'((int(*)(void*)){funcs["il2cpp_field_get_offset"]})((void*){field_ptr})')
                    result.PutCString(f"FOUND: {ns}.{class_name}.{field_name} (offset={offset})")

                field_count += 1

            # Free iterator per class (not really needed but cleaner)
            # Keep it allocated

        _call(debugger, frame, f'(void)free((void*){iter_ptr})')

    result.PutCString("DONE")


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand('command script add -f lldb_search_checksum.search_checksum_fields search_checksum_fields')