#!/usr/bin/env python3
"""LLDB Python script to find the Configuration class by iterating all classes."""

import lldb


def _call(debugger, frame, expr):
    """Evaluate an expression and return the unsigned value, or None on error."""
    eval_result = frame.EvaluateExpression(expr)
    if eval_result.GetError().Fail():
        stream = lldb.SBStream()
        eval_result.GetError().GetDescription(stream)
        return None
    return eval_result.GetValueAsUnsigned()


def find_configuration_class(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    process = target.GetProcess()
    thread = process.GetSelectedThread()
    frame = thread.GetSelectedFrame()

    base_addr = 0x11e1d4000

    funcs = {
        'il2cpp_image_get_name': base_addr + 0x587da0,
        'il2cpp_image_get_class_count': base_addr + 0x587dac,
        'il2cpp_image_get_class': base_addr + 0x587dc4,
        'il2cpp_class_get_name': base_addr + 0x586fb0,
        'il2cpp_class_get_namespace': base_addr + 0x586fb4,
    }

    domain_get = base_addr + 0x587584
    assembly_open = base_addr + 0x587588
    image_get = base_addr + 0x586f4c

    # Get domain with proper cast
    domain = _call(debugger, frame, f"((void*(*)(void)){domain_get})()")
    if domain is None:
        result.PutCString("ERROR: Could not get domain")
        return
    result.PutCString(f"domain = 0x{domain:x}")

    # Open assemblies
    assemblies = []
    for asm_name in ["Assembly-CSharp", "Assembly-CSharp-firstpass", "SharedModel", "Model", "SavySoda.PixelStarships", "Configuration"]:
        asm = _call(debugger, frame, f'((void*(*)(void*, const char*)){assembly_open})((void*){domain}, "{asm_name}")')
        if asm is not None and asm != 0:
            assemblies.append((asm_name, asm))

    result.PutCString(f"Found {len(assemblies)} assemblies:")
    for name, addr in assemblies:
        img = _call(debugger, frame, f'((void*(*)(void*)){image_get})((void*){addr})')
        if img is not None and img != 0:
            name_ptr = _call(debugger, frame, f'((const char*(*)(void*)){funcs["il2cpp_image_get_name"]})((void*){img})')
            if name_ptr is not None and name_ptr != 0:
                error = lldb.SBError()
                raw = process.ReadMemory(name_ptr, 256, error)
                if not error.Fail():
                    img_name = raw.split(b'\x00')[0].decode('utf-8', errors='ignore')
                    result.PutCString(f"  {name}: image=0x{img:x}, name={img_name}")

    # Search for Configuration in each image
    for asm_name, asm_addr in assemblies:
        img = _call(debugger, frame, f'((void*(*)(void*)){image_get})((void*){asm_addr})')
        if img is None or img == 0:
            continue

        count = _call(debugger, frame, f'((int(*)(void*)){funcs["il2cpp_image_get_class_count"]})((void*){img})')
        if count is None:
            continue

        result.PutCString(f"Searching {count} classes in {asm_name}...")

        for i in range(count):
            klass = _call(debugger, frame, f'((void*(*)(void*, int)){funcs["il2cpp_image_get_class"]})((void*){img}, {i})')
            if klass is None or klass == 0:
                continue

            name_ptr = _call(debugger, frame, f'((const char*(*)(void*)){funcs["il2cpp_class_get_name"]})((void*){klass})')
            if name_ptr is None or name_ptr == 0:
                continue

            error = lldb.SBError()
            raw = process.ReadMemory(name_ptr, 256, error)
            if error.Fail():
                continue
            class_name = raw.split(b'\x00')[0].decode('utf-8', errors='ignore')

            if class_name == "Configuration":
                ns_ptr = _call(debugger, frame, f'((const char*(*)(void*)){funcs["il2cpp_class_get_namespace"]})((void*){klass})')
                if ns_ptr is not None and ns_ptr != 0:
                    raw = process.ReadMemory(ns_ptr, 256, error)
                    if not error.Fail():
                        ns = raw.split(b'\x00')[0].decode('utf-8', errors='ignore')
                    else:
                        ns = ""
                else:
                    ns = ""

                result.PutCString(f"FOUND: Configuration class at 0x{klass:x}, namespace='{ns}', assembly='{asm_name}'")
                return

    result.PutCString("Configuration class NOT FOUND in any assembly")


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand('command script add -f lldb_find_config.find_configuration_class find_configuration_class')