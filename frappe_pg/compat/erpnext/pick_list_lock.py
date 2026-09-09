"""Backport ERPNext's PostgreSQL-safe Pick List grouped locking semantics."""

import inspect
from importlib import import_module

NAME = "erpnext_pick_list_lock"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.stock.doctype.pick_list.pick_list")
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
        and ".select(pi_item.name)" in source
        and ".for_update().run()" in source
        and "query = query.for_update()" in source
    )


def _legacy_shape_supported(method):
    source = _source(method)
    return (
        "get_picked_items_qty" in source
        and "Pick List Item" in source
        and "Sum(" in source
        and ".for_update()" in source
    )


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_picked_items_qty is _patched


def is_needed():
    module = _load()
    method = getattr(module, "get_picked_items_qty", None) if module else None
    return (
        method is not None
        and not is_applied()
        and _legacy_shape_supported(method)
        and not _upstream_is_compatible(method)
    )


def _compatible(items, contains_packed_items=False):
    import frappe
    from frappe.query_builder.functions import Max, Sum

    if frappe.db.db_type != "postgres":
        return _original(items, contains_packed_items)

    pi_item = frappe.qb.DocType("Pick List Item")
    group_field = pi_item.product_bundle_item if contains_packed_items else pi_item.sales_order_item
    conditions = (pi_item.docstatus == 1) & group_field.isin(items)
    query = (
        frappe.qb.from_(pi_item)
        .select(
            Max(pi_item.sales_order_item).as_("sales_order_item"),
            Max(pi_item.product_bundle_item).as_("product_bundle_item"),
            Max(pi_item.item_code).as_("item_code"),
            pi_item.sales_order,
            Sum(pi_item.stock_qty).as_("stock_qty"),
            Sum(pi_item.picked_qty).as_("picked_qty"),
        )
        .where(conditions)
        .groupby(group_field, pi_item.sales_order)
    )
    # PostgreSQL cannot lock a grouped aggregate. Lock the same underlying
    # detail rows first; row locks remain held for the surrounding transaction.
    frappe.qb.from_(pi_item).select(pi_item.name).where(conditions).for_update().run()
    return query.run(as_dict=True)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.get_picked_items_qty
    _patched = _compatible
    module.get_picked_items_qty = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_picked_items_qty = _original  # nosemgrep
    _original = None
    _patched = None
    return True
