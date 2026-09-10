"""Backport ERPNext reserved-qty semantics that avoid PostgreSQL division by zero."""

import inspect
from importlib import import_module

NAME = "erpnext_stock_reserved_qty"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.stock.stock_balance")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _needs_patch(method):
    source = _source(method)
    return "so_item_qty" in source and "/ so_item_qty" in source and "so_item.qty != 0" not in source


def _compatible(item_code, warehouse):
    import frappe
    from frappe.query_builder.functions import Sum
    from frappe.utils import flt

    dont_reserve_on_return = frappe.get_cached_value(
        "Selling Settings", "Selling Settings", "dont_reserve_sales_order_qty_on_sales_return"
    )
    so = frappe.qb.DocType("Sales Order")
    so_item = frappe.qb.DocType("Sales Order Item")
    packed_item = frappe.qb.DocType("Packed Item")

    open_so = (so.docstatus == 1) & so.status.notin(["On Hold", "Closed"])
    not_delivered_by_supplier = so_item.delivered_by_supplier.isnull() | (so_item.delivered_by_supplier == 0)
    not_closed = so_item.closed.isnull() | (so_item.closed == 0)
    reservable = (so_item.qty != 0) & (so_item.qty >= so_item.delivered_qty)
    net_reserved = (
        so_item.qty - so_item.delivered_qty - so_item.returned_qty
        if dont_reserve_on_return
        else so_item.qty - so_item.delivered_qty
    )

    packed_qty = (
        frappe.qb.from_(packed_item)
        .inner_join(so)
        .on(so.name == packed_item.parent)
        .inner_join(so_item)
        .on(so_item.name == packed_item.parent_detail_docname)
        .select(Sum(packed_item.qty * net_reserved / so_item.qty))
        .where(
            (packed_item.item_code == item_code)
            & (packed_item.warehouse == warehouse)
            & (packed_item.parenttype == "Sales Order")
            & (packed_item.item_code != packed_item.parent_item)
            & not_delivered_by_supplier
            & not_closed
            & open_so
            & reservable
        )
        .run()
    )
    direct_qty = (
        frappe.qb.from_(so_item)
        .inner_join(so)
        .on(so.name == so_item.parent)
        .select(Sum(so_item.stock_qty * net_reserved / so_item.qty))
        .where(
            (so_item.item_code == item_code)
            & (so_item.warehouse == warehouse)
            & not_delivered_by_supplier
            & not_closed
            & open_so
            & reservable
        )
        .run()
    )
    return flt(packed_qty[0][0]) + flt(direct_qty[0][0])


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_reserved_qty is _patched


def is_needed():
    module = _load()
    method = getattr(module, "get_reserved_qty", None) if module else None
    return method is not None and not is_applied() and _needs_patch(method)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.get_reserved_qty
    _patched = _compatible
    module.get_reserved_qty = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_reserved_qty = _original  # nosemgrep
    _original = None
    _patched = None
    return True
