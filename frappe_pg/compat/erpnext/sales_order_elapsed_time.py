"""Backport PostgreSQL-safe Sales Order Analysis elapsed-time calculation."""

import inspect
from collections import OrderedDict
from importlib import import_module

NAME = "erpnext_sales_order_elapsed_time"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.selling.report.sales_order_analysis.sales_order_analysis")
    except ImportError:
        return None


def _source(function):
    try:
        return inspect.getsource(function)
    except (OSError, TypeError):
        return ""


def _needs_patch(function):
    source = _source(function)
    return 'CustomFunction("TO_SECONDS"' in source and 'frappe.db.db_type == "postgres"' not in source


def _compatible(data):
    import frappe
    from frappe import qb
    from frappe.query_builder.functions import Max

    if frappe.db.db_type != "postgres":
        return _original(data)

    elapsed = OrderedDict()
    if not data:
        return elapsed

    sales_orders = [row.sales_order for row in data]
    so = qb.DocType("Sales Order")
    soi = qb.DocType("Sales Order Item")
    dn = qb.DocType("Delivery Note")
    dni = qb.DocType("Delivery Note Item")

    elapsed_seconds = ((Max(dn.posting_date) - so.transaction_date) * 86400).as_("elapsed_seconds")
    rows = (
        qb.from_(so)
        .inner_join(soi)
        .on(soi.parent == so.name)
        .left_join(dni)
        .on(dni.so_detail == soi.name)
        .left_join(dn)
        .on(dni.parent == dn.name)
        .select(
            so.name.as_("sales_order"),
            soi.item_code.as_("so_item_code"),
            elapsed_seconds,
        )
        .where((so.name.isin(sales_orders)) & (dn.docstatus == 1))
        .orderby(so.name, soi.name)
        .groupby(soi.name, so.name)
    ).run(as_dict=True)

    for row in rows:
        elapsed[(row.sales_order, row.so_item_code)] = row.elapsed_seconds
    return elapsed


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_so_elapsed_time is _patched


def is_needed():
    module = _load()
    function = getattr(module, "get_so_elapsed_time", None) if module else None
    return function is not None and not is_applied() and _needs_patch(function)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.get_so_elapsed_time
    _patched = _compatible
    module.get_so_elapsed_time = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_so_elapsed_time = _original  # nosemgrep
    _original = None
    _patched = None
    return True
