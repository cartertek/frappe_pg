"""Backport PostgreSQL-safe ERPNext stock reservation grouping semantics."""

import inspect
from importlib import import_module

NAME = "erpnext_stock_reservation_grouping"
_original = None
_patched = None


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


def _needs_patch(method):
    source = _source(method)
    return (
        "def get_items_to_reserve" in source
        and ".groupby(child_doctype.name)" in source
        and 'doctype.name.as_("voucher_no")' in source
        and 'Max(doctype.name).as_("voucher_no")' not in source
    )


def _compatible(self, docnames, from_doctype, to_doctype):
    import frappe
    from frappe.query_builder.functions import Max
    from frappe.utils import flt

    field = frappe.scrub(from_doctype)
    item_code_fieldname, child_table_suffix = (
        ("rm_item_code", " Supplied Item") if to_doctype == "Subcontracting Order" else ("item_code", " Item")
    )
    doctype = frappe.qb.DocType(to_doctype)
    child_doctype = frappe.qb.DocType(to_doctype + child_table_suffix)

    query = (
        frappe.qb.from_(doctype)
        .inner_join(child_doctype)
        .on(doctype.name == child_doctype.parent)
        .select(
            Max(doctype.name).as_("voucher_no"),
            child_doctype.name.as_("voucher_detail_no"),
            child_doctype[item_code_fieldname].as_("item_code"),
            Max(doctype.company).as_("company"),
            child_doctype.stock_uom,
        )
        .where((doctype.docstatus == 1) & (doctype[field].isin(docnames)))
        .groupby(child_doctype.name)
    )

    if to_doctype == "Work Order":
        query = query.select(
            child_doctype.source_warehouse,
            Max(doctype.wip_warehouse).as_("wip_warehouse"),
            Max(doctype.skip_transfer).as_("skip_transfer"),
            Max(doctype.from_wip_warehouse).as_("from_wip_warehouse"),
            child_doctype.required_qty,
            (child_doctype.required_qty - child_doctype.transferred_qty).as_("qty"),
            child_doctype.stock_reserved_qty,
        ).where(
            (doctype.qty > doctype.material_transferred_for_manufacturing) & (doctype.status != "Completed")
        )
    elif to_doctype == "Subcontracting Order":
        query = query.select(
            child_doctype.stock_reserved_qty,
            child_doctype.required_qty.as_("qty"),
            child_doctype.reserve_warehouse.as_("source_warehouse"),
        )

    if self.items and (data := [item.voucher_detail_no for item in self.items]):
        query = query.where(child_doctype.name.isin(data))

    items = []
    for row in query.run(as_dict=True):
        if row.qty > row.stock_reserved_qty:
            row.qty -= flt(row.stock_reserved_qty)
            row.warehouse = row.source_warehouse
            if row.skip_transfer and row.from_wip_warehouse:
                row.warehouse = row.wip_warehouse
            if to_doctype == "Work Order":
                row.voucher_type = "Work Order"
            items.append(row)
    return items


def is_applied():
    module = _load()
    cls = getattr(module, "StockReservation", None) if module else None
    return cls is not None and _patched is not None and cls.get_items_to_reserve is _patched


def is_needed():
    module = _load()
    cls = getattr(module, "StockReservation", None) if module else None
    method = getattr(cls, "get_items_to_reserve", None) if cls else None
    return method is not None and not is_applied() and _needs_patch(method)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    cls = module.StockReservation
    _original = cls.get_items_to_reserve
    _patched = _compatible
    cls.get_items_to_reserve = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    cls = getattr(module, "StockReservation", None) if module else None
    if cls is None or not is_applied():
        return False
    cls.get_items_to_reserve = _original  # nosemgrep
    _original = None
    _patched = None
    return True
