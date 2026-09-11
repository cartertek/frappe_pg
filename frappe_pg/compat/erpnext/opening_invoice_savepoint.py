"""Backport transaction-safe Opening Invoice imports for PostgreSQL."""

import inspect
from importlib import import_module

NAME = "erpnext_opening_invoice_savepoint"
_original = None
_patched = None


def _load():
    try:
        return import_module(
            "erpnext.accounts.doctype.opening_invoice_creation_tool.opening_invoice_creation_tool"
        )
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _needs_patch(method):
    source = _source(method)
    return "frappe.db.rollback()" in source and "doc.log_error" in source and "save_point=" not in source


def _compatible_start_import(invoices):
    import frappe
    from frappe import _

    module = _load()
    errors = 0
    names = []
    total = len(invoices)
    for idx, d in enumerate(invoices):
        savepoint = f"opening_invoice_{frappe.generate_hash(length=8)}"
        frappe.db.savepoint(savepoint)
        is_last = idx == total - 1
        try:
            invoice_number = None
            if d.invoice_number:
                invoice_number = d.invoice_number
            doc = frappe.get_doc(d)
            doc.flags.ignore_mandatory = True
            doc.flags.dont_auto_add_taxes = True
            doc.insert(set_name=invoice_number)
            doc.submit()
            if not frappe.in_test:
                frappe.db.commit()
            names.append(doc.name)
            module.publish(idx, total, d.doctype, errors=errors if is_last else None)
        except Exception:
            errors += 1
            frappe.db.rollback(save_point=savepoint)
            doc.log_error("Opening invoice creation failed")
            module.publish(idx, total, d.doctype, errors=errors if is_last else None)

    if errors:
        frappe.msgprint(
            _("You had {0} errors while creating opening invoices. Check {1} for more details").format(
                errors, "<a href='/app/List/Error Log' class='variant-click'>Error Log</a>"
            ),
            indicator="red",
            title=_("Error Occurred"),
        )
    return names


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.start_import is _patched


def is_needed():
    module = _load()
    method = getattr(module, "start_import", None) if module else None
    return method is not None and not is_applied() and _needs_patch(method)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.start_import
    _patched = _compatible_start_import
    module.start_import = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.start_import = _original  # nosemgrep
    _original = None
    _patched = None
    return True
