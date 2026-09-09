"""Backport ERPNext's PostgreSQL-safe Stock Reservation locking semantics."""

import inspect
from importlib import import_module

NAME = "erpnext_stock_reservation_lock"
_original = None
_patched = None
_uses_extended_qty = False
_uses_status_filter = False


def _load():
    try:
        return import_module("erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _upstream_is_compatible(method):
    source = _source(method)
    return (
        'frappe.db.db_type == "postgres"' in source
        and ".select(sre.name)" in source
        and ".for_update().run()" in source
        and "query = query.for_update()" in source
    )


def _legacy_shape_supported(method):
    source = _source(method)
    return (
        "get_available_qty_to_reserve" in source
        and "Stock Reservation Entry" in source
        and "Sum(" in source
        and ".for_update()" in source
    )


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_available_qty_to_reserve is _patched


def is_needed():
    module = _load()
    method = getattr(module, "get_available_qty_to_reserve", None) if module else None
    return (
        method is not None
        and not is_applied()
        and _legacy_shape_supported(method)
        and not _upstream_is_compatible(method)
    )


def _compatible(item_code: str, warehouse: str, batch_no: str | None = None, ignore_sre=None) -> float:
    import frappe
    from frappe.query_builder.functions import Sum

    module = _load()
    from erpnext.stock.doctype.batch.batch import get_batch_qty

    if batch_no:
        return get_batch_qty(
            item_code=item_code,
            warehouse=warehouse,
            batch_no=batch_no,
            ignore_voucher_nos=[ignore_sre],
        )

    available_qty = module.get_stock_balance(item_code, warehouse)
    if not available_qty:
        return available_qty

    sre = frappe.qb.DocType("Stock Reservation Entry")
    conditions = (
        (sre.docstatus == 1)
        & (sre.item_code == item_code)
        & (sre.warehouse == warehouse)
        & (sre.delivered_qty < sre.reserved_qty)
    )
    if _uses_status_filter:
        conditions &= sre.status.notin(["Delivered", "Cancelled"])
    if ignore_sre:
        conditions &= sre.name != ignore_sre

    # PostgreSQL rejects FOR UPDATE on aggregate queries. Serialize the
    # calculation on the Bin row (covering the no-SRE case) and then lock every
    # matching reservation row individually for the surrounding transaction.
    bin_table = frappe.qb.DocType("Bin")
    (
        frappe.qb.from_(bin_table)
        .select(bin_table.name)
        .where((bin_table.item_code == item_code) & (bin_table.warehouse == warehouse))
        .for_update()
        .run()
    )
    frappe.qb.from_(sre).select(sre.name).where(conditions).orderby(sre.name).for_update().run()

    if _uses_extended_qty:
        reserved_expr = sre.reserved_qty - sre.delivered_qty - sre.transferred_qty - sre.consumed_qty
    else:
        reserved_expr = sre.reserved_qty - sre.delivered_qty
    reserved_qty = frappe.qb.from_(sre).select(Sum(reserved_expr)).where(conditions).run()[0][0] or 0.0
    return available_qty - reserved_qty if reserved_qty else available_qty


def apply():
    global _original, _patched, _uses_extended_qty, _uses_status_filter
    if not is_needed():
        return False
    module = _load()
    _original = module.get_available_qty_to_reserve
    source = _source(_original)
    _uses_extended_qty = "transferred_qty" in source and "consumed_qty" in source
    _uses_status_filter = ".status.notin" in source
    _patched = _compatible
    module.get_available_qty_to_reserve = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched, _uses_extended_qty, _uses_status_filter
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_available_qty_to_reserve = _original  # nosemgrep
    _original = None
    _patched = None
    _uses_extended_qty = False
    _uses_status_filter = False
    return True
