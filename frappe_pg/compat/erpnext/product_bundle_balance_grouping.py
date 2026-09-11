"""Backport PostgreSQL-safe Product Bundle Balance grouping from ERPNext develop."""

import inspect
from importlib import import_module

NAME = "erpnext_product_bundle_balance_grouping"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.stock.report.product_bundle_balance.product_bundle_balance")
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
        "sle.name" in source
        and "Max(sle.posting_datetime)" in source
        and ".groupby(sle.item_code, sle.warehouse)" in source
    )


def _compatible(filters, items):
    import frappe
    from frappe.query_builder.functions import Max

    sle = frappe.qb.DocType("Stock Ledger Entry")
    query = (
        frappe.qb.from_(sle)
        .select(sle.item_code, sle.warehouse, Max(sle.posting_datetime).as_("posting_datetime"))
        .where(sle.item_code.isin(items) & (sle.is_cancelled == 0))
        .groupby(sle.item_code, sle.warehouse)
    )

    if filters.get("warehouse"):
        warehouse_details = frappe.db.get_value(
            "Warehouse", filters.get("warehouse"), ["lft", "rgt"], as_dict=1
        )
        if warehouse_details:
            wh = frappe.qb.DocType("Warehouse")
            query = query.where(
                sle.warehouse.isin(
                    frappe.qb.from_(wh)
                    .select(wh.name)
                    .where((wh.lft >= warehouse_details.lft) & (wh.rgt <= warehouse_details.rgt))
                )
            )

    if filters.get("company"):
        query = query.where(sle.company == filters.get("company"))

    if filters.get("data"):
        query = query.where(sle.posting_date <= filters.get("data"))

    return query


def is_applied():
    module = _load()
    return (
        module is not None and _patched is not None and module.get_item_wise_max_posting_datetime is _patched
    )


def is_needed():
    module = _load()
    method = getattr(module, "get_item_wise_max_posting_datetime", None) if module else None
    return method is not None and not is_applied() and _needs_patch(method)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.get_item_wise_max_posting_datetime
    _patched = _compatible
    module.get_item_wise_max_posting_datetime = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_item_wise_max_posting_datetime = _original  # nosemgrep
    _original = None
    _patched = None
    return True
