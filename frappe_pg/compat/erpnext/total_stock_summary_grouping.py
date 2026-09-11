"""Backport PostgreSQL-safe Total Stock Summary grouping from ERPNext develop."""

import inspect
from importlib import import_module

NAME = "erpnext_total_stock_summary_grouping"
UPSTREAM_COMMIT = "develop"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.stock.report.total_stock_summary.total_stock_summary")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_total_stock is _patched


def is_needed():
    module = _load()
    method = getattr(module, "get_total_stock", None) if module else None
    if method is None or is_applied():
        return False
    source = _source(method)
    return (
        "item.description" in source
        and "Sum(bin.actual_qty)" in source
        and ".groupby(" in source
        and "Max(item.description)" not in source
    )


def _compatible(filters):
    import frappe
    from frappe.query_builder.functions import Max, Sum

    bin_table = frappe.qb.DocType("Bin")
    item = frappe.qb.DocType("Item")
    warehouse = frappe.qb.DocType("Warehouse")

    query = (
        frappe.qb.from_(bin_table)
        .inner_join(item)
        .on(bin_table.item_code == item.item_code)
        .inner_join(warehouse)
        .on(warehouse.name == bin_table.warehouse)
        .where(bin_table.actual_qty != 0)
    )

    if filters.get("group_by") == "Warehouse":
        if filters.get("company"):
            query = query.where(warehouse.company == filters.get("company"))
        query = query.select(bin_table.warehouse).groupby(bin_table.warehouse)
    else:
        query = query.select(warehouse.company).groupby(warehouse.company)

    query = query.select(
        item.item_code,
        Max(item.description).as_("description"),
        Sum(bin_table.actual_qty).as_("actual_qty"),
    ).groupby(item.item_code)

    return query.run()


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.get_total_stock
    _patched = _compatible
    module.get_total_stock = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_total_stock = _original  # nosemgrep
    _original = None
    _patched = None
    return True
