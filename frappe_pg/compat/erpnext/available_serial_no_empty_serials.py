"""Backport ERPNext's null-safe Available Serial No accumulation."""

import inspect
from importlib import import_module

NAME = "erpnext_available_serial_no_empty_serials"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.stock.report.available_serial_no.available_serial_no")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _needs_patch(method):
    source = _source(method)
    return (
        'sle.balance_serial_no = "\\n".join(serial_nos)' in source
        and '"\\n".join(serial_nos) if serial_nos else ""' not in source
    )


def _compatible(available_serial_nos, sle):
    module = _load()
    serial_nos = (
        module.get_serial_nos(sle.serial_no)
        if sle.serial_no
        else available_serial_nos.get(sle.serial_and_batch_bundle)
    )
    key = (sle.item_code, sle.warehouse)
    sle.serial_no = "\n".join(serial_nos) if serial_nos else ""
    if key not in available_serial_nos:
        available_serial_nos.setdefault(key, serial_nos)
        sle.balance_serial_no = "\n".join(serial_nos) if serial_nos else ""
        return

    existing_serial_no = available_serial_nos[key]
    for sn in serial_nos or ():
        if sn in existing_serial_no:
            existing_serial_no.remove(sn)
        else:
            existing_serial_no.append(sn)

    sle.balance_serial_no = "\n".join(existing_serial_no or ())


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.update_available_serial_nos is _patched


def is_needed():
    module = _load()
    method = getattr(module, "update_available_serial_nos", None) if module else None
    return method is not None and not is_applied() and _needs_patch(method)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.update_available_serial_nos
    _patched = _compatible
    module.update_available_serial_nos = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.update_available_serial_nos = _original  # nosemgrep
    _original = None
    _patched = None
    return True
