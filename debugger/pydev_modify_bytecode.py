import dis
from opcode import opmap, EXTENDED_ARG
from queue import Queue
from types import CodeType

MAX_BYTE = 255


def _add_attr_values_from_insert_to_original(original_code, insert_code, insert_code_obj, attribute_name, op_list):
    """
    This function appends values of the attribute `attribute_name` of the inserted code to the original values,
     and changes indexes inside inserted code. If some bytecode instruction in the inserted code used to call argument
     number i, after modification it calls argument n + i, where n - length of the values in the original code.
     So it helps to avoid variables mixing between two pieces of code.

    :param original_code: code to modify
    :param insert_code: code to insert
    :param insert_code_obj: bytes sequence of inserted code, which should be modified too
    :param attribute_name: name of attribute to modify ('co_names', 'co_consts' or 'co_varnames')
    :param op_list: sequence of bytecodes whose arguments should be changed
    :return: modified bytes sequence of the code to insert and new values of the attribute `attribute_name` for original code
    """
    orig_value = getattr(original_code, attribute_name)
    insert_value = getattr(insert_code, attribute_name)
    orig_names_len = len(orig_value)
    code_with_new_values = list(insert_code_obj)
    for offset, op, arg in dis._unpack_opargs(insert_code_obj):
        if op in op_list:
            if code_with_new_values[offset + 1] + orig_names_len > MAX_BYTE:
                raise ValueError("Bad number of arguments")
            code_with_new_values[offset + 1] += orig_names_len
    new_values = orig_value + insert_value
    return bytes(code_with_new_values), new_values


def _modify_new_lines(code_to_modify, all_inserted_code):
    """
    Update new lines in order to hide inserted code inside the original code
    :param code_to_modify: code to modify
    :param code_insert: code to insert
    :param offset_of_inserted_code: the offset of the inserted code
    :return: bytes sequence of code with updated lines offsets
    """
    new_list = list(code_to_modify.co_lnotab)
    abs_offset = 0
    for i in range(0, len(new_list), 2):
        prev_abs_offset = abs_offset
        abs_offset += new_list[i]
        for inserted_offset in all_inserted_code:
            if prev_abs_offset <= inserted_offset < abs_offset:
                if new_list[i] + all_inserted_code[inserted_offset] > MAX_BYTE:
                    raise ValueError("Bad number of arguments")
                size_of_inserted = all_inserted_code[inserted_offset]
                new_list[i] += size_of_inserted
                abs_offset += size_of_inserted
    return bytes(new_list)


def _update_label_offsets(code_obj, breakpoint_offset, breakpoint_code_list):
    """
    Update labels for the relative and absolute jump targets
    :param code_obj: code to modify
    :param offset_of_inserted_code: offset for the inserted code
    :param offset_of_inserted_code: size of the inserted code
    :return: bytes sequence with modified labels
    """
    pieces_of_code_to_insert = Queue()
    pieces_of_code_to_insert.put((breakpoint_offset, breakpoint_code_list))
    # During offsets updating one of them can become > max byte value. In this case we need to insert the new operator and
    # update arguments for relative and absolute jumps again. We will use this queue for saving pieces of code which should be
    # inserted into the code by turns.
    all_inserted_code = dict()
    all_inserted_code[breakpoint_offset] = len(breakpoint_code_list)
    code_list = list(code_obj)

    while not pieces_of_code_to_insert.empty():
        current_offset, current_code_list = pieces_of_code_to_insert.get()
        offsets_for_modification = []

        for offset, op, arg in dis._unpack_opargs(code_list):
            if arg is not None:
                if op in dis.hasjrel:
                    # has relative jump target
                    label = offset + 2 + arg
                    if offset < current_offset < label:
                        # change labels for relative jump targets if code was inserted inside
                        offsets_for_modification.append(offset)
                elif op in dis.hasjabs:
                    # change label for absolute jump if code was inserted before it
                    if current_offset <= arg:
                        offsets_for_modification.append(offset)
        for i in range(0, len(code_list), 2):
            op = code_list[i]
            if i in offsets_for_modification and op >= dis.HAVE_ARGUMENT:
                new_arg = code_list[i + 1] + len(current_code_list)
                if new_arg <= MAX_BYTE:
                    code_list[i + 1] = new_arg
                else:
                    # if new argument > 255 we need to insert the new operator EXTENDED_ARG
                    extended_arg_code = [EXTENDED_ARG, new_arg >> 8]
                    code_list[i + 1] = new_arg & MAX_BYTE
                    pieces_of_code_to_insert.put((i, extended_arg_code))
                    all_inserted_code[i] = len(extended_arg_code)

        code_list = code_list[:current_offset] + current_code_list + code_list[current_offset:]

        all_inserted_code_new = dict()
        for offset_inserted in all_inserted_code:
            code_size = all_inserted_code[offset_inserted]
            if current_offset < offset_inserted:
                all_inserted_code_new[offset_inserted + len(current_code_list)] = code_size
            else:
                all_inserted_code_new[offset_inserted] = code_size
        all_inserted_code = all_inserted_code_new

        pieces_of_code_to_insert_new = Queue()
        while not pieces_of_code_to_insert.empty():
            offset_of_code, inserted_code_list = pieces_of_code_to_insert.get()
            if current_offset < offset_of_code:
                pieces_of_code_to_insert_new.put((offset_of_code + len(current_code_list), inserted_code_list))
            else:
                pieces_of_code_to_insert_new.put((offset_of_code, inserted_code_list))
        pieces_of_code_to_insert = pieces_of_code_to_insert_new

    return bytes(code_list), all_inserted_code


def _return_none_fun():
    return None


def insert_code(code_to_modify, code_to_insert, before_line):
    """
    Insert piece of code `code_to_insert` to `code_to_modify` right inside the line `before_line` before the
    instruction on this line by modifying original bytecode

    :param code_to_modify: Code to modify
    :param code_to_insert: Code to insert
    :param before_line: Number of line for code insertion
    :return: modified code
    """
    dis.dis(code_to_modify)
    linestarts = dict(dis.findlinestarts(code_to_modify))
    if before_line not in linestarts.values():
        return code_to_modify
    offset = None
    for off, line_no in linestarts.items():
        if line_no == before_line:
            offset = off

    return_none_size = len(_return_none_fun.__code__.co_code)
    code_to_insert_obj = code_to_insert.co_code[:-return_none_size]
    try:
        code_to_insert_obj, new_names = \
            _add_attr_values_from_insert_to_original(code_to_modify, code_to_insert, code_to_insert_obj, 'co_names', dis.hasname)
        code_to_insert_obj, new_consts = \
            _add_attr_values_from_insert_to_original(code_to_modify, code_to_insert, code_to_insert_obj, 'co_consts', [opmap['LOAD_CONST']])
        code_to_insert_obj, new_vars = \
            _add_attr_values_from_insert_to_original(code_to_modify, code_to_insert, code_to_insert_obj, 'co_varnames', dis.haslocal)
        new_bytes, all_inserted_code = _update_label_offsets(code_to_modify.co_code, offset, list(code_to_insert_obj))

        new_lnotab = _modify_new_lines(code_to_modify, all_inserted_code)
    except ValueError:
        raise

    new_code = CodeType(
        code_to_modify.co_argcount,  # integer
        code_to_modify.co_kwonlyargcount,  # integer
        len(new_vars),  # integer
        code_to_modify.co_stacksize,  # integer
        code_to_modify.co_flags,  # integer
        new_bytes,  # bytes
        new_consts,  # tuple
        new_names,  # tuple
        new_vars,  # tuple
        code_to_modify.co_filename,  # string
        code_to_modify.co_name,  # string
        code_to_modify.co_firstlineno,  # integer
        new_lnotab,  # bytes
        code_to_modify.co_freevars,  # tuple
        code_to_modify.co_cellvars  # tuple
    )
    dis.dis(new_code)
    return True, new_code
