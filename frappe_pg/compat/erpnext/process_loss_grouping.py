"""Backport ERPNext's PostgreSQL-safe Process Loss Report grouping."""

import inspect
from importlib import import_module

NAME = "erpnext_process_loss_grouping"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.manufacturing.report.process_loss_report.process_loss_report")
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
        ".groupby(se.work_order)" in source
        and "wo.name," in source
        and "Max(wo.name)" not in source
        and "Sum(se.total_incoming_value)" in source
    )


def _compatible_get_data(filters):
    import frappe
    from frappe.query_builder.functions import Max, Sum

    filters = frappe._dict(filters or {})
    wo = frappe.qb.DocType("Work Order")
    se = frappe.qb.DocType("Stock Entry")

    query = (
        frappe.qb.from_(wo)
        .inner_join(se)
        .on(wo.name == se.work_order)
        .select(
            Max(wo.name).as_("name"),
            Max(wo.status).as_("status"),
            Max(wo.production_item).as_("production_item"),
            Max(wo.produced_qty).as_("produced_qty"),
            Max(wo.process_loss_qty).as_("process_loss_qty"),
            Max(wo.qty).as_("qty_to_manufacture"),
            Sum(se.total_incoming_value).as_("total_fg_value"),
            Sum(se.total_outgoing_value).as_("total_rm_value"),
        )
        .where(
            (wo.process_loss_qty > 0)
            & (wo.company == filters.company)
            & (se.docstatus == 1)
            & (se.purpose == "Manufacture")
            & (se.posting_date.between(filters.from_date, filters.to_date))
        )
        .groupby(se.work_order)
    )

    if filters.get("item"):
        query = query.where(wo.production_item == filters.item)
    if filters.get("work_order"):
        query = query.where(wo.name == filters.work_order)

    data = query.run(as_dict=True)
    module = _load()
    module.update_data_with_total_pl_value(data)
    return data


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_data is _patched


def is_needed():
    module = _load()
    method = getattr(module, "get_data", None) if module else None
    return method is not None and not is_applied() and _needs_patch(method)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.get_data
    _patched = _compatible_get_data
    module.get_data = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_data = _original  # nosemgrep
    _original = None
    _patched = None
    return True
