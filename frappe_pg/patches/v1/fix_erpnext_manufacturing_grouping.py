"""Backport PostgreSQL-safe ERPNext manufacturing grouped-query semantics."""

import inspect
from importlib import import_module

_original_job_card = None
_original_manufacture = None
_patched_job_card = None
_patched_manufacture = None


def _load_stock_entry():
    try:
        return import_module("erpnext.stock.doctype.stock_entry.stock_entry")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _needs_job_card_patch(cls):
    method = getattr(cls, "get_secondary_items_from_job_card", None)
    if method is None:
        return False
    source = _source(method)
    return (
        "Job Card Secondary Item" in source
        and "job_card_secondary_item.item_name" in source
        and "Max(job_card_secondary_item.stock_uom)" not in source
    )


def _needs_manufacture_patch(cls):
    method = getattr(cls, "get_items_from_manufacture_stock_entry", None)
    if method is None:
        return False
    source = _source(method)
    return (
        'Sum(SED.qty).as_("qty")' in source
        and ".groupby(SED.item_code)" in source
        and "get_representative_manufacture_rows" not in source
    )


def _compatible_job_card(self):
    import frappe
    from frappe.query_builder.functions import Max, Min, Sum
    from frappe.utils import cint, flt

    if not hasattr(self, "pro_doc"):
        self.pro_doc = None
    if not self.pro_doc:
        self.set_work_order_details()
    if not self.pro_doc.operations:
        return []

    job_card = frappe.qb.DocType("Job Card")
    secondary = frappe.qb.DocType("Job Card Secondary Item")
    query = (
        frappe.qb.from_(job_card)
        .select(
            Sum(secondary.stock_qty).as_("stock_qty"),
            secondary.item_code,
            Max(secondary.stock_uom).as_("stock_uom"),
            secondary.secondary_item_type,
            secondary.bom_secondary_item,
        )
        .join(secondary)
        .on(secondary.parent == job_card.name)
        .where(
            (secondary.item_code.isnotnull())
            & (job_card.work_order == self.work_order)
            & (job_card.docstatus == 1)
        )
        .groupby(secondary.item_code, secondary.secondary_item_type, secondary.bom_secondary_item)
        .orderby(Min(secondary.idx))
    )
    if self.job_card:
        query = query.where(job_card.name == self.job_card)
    rows = query.run(as_dict=1)

    job_cards = frappe.get_all(
        "Job Card",
        filters={
            "work_order": self.work_order,
            "docstatus": 1,
            **({"name": self.job_card} if self.job_card else {}),
        },
        pluck="name",
    )
    representative = {}
    if job_cards:
        for line in frappe.get_all(
            "Job Card Secondary Item",
            filters={"parent": ("in", job_cards)},
            fields=["item_code", "secondary_item_type", "item_name", "description"],
            order_by="idx, creation",
        ):
            representative.setdefault((line.item_code, line.secondary_item_type), line)
    for row in rows:
        line = representative.get((row.item_code, row.secondary_item_type))
        row.item_name = line.item_name if line else None
        row.description = line.description if line else None

    pending_qty = (
        flt(self.fg_completed_qty)
        if self.job_card
        else flt(self.get_completed_job_card_qty()) - flt(self.pro_doc.produced_qty)
    )
    used_secondary_items = self.get_used_secondary_items()
    module = _load_stock_entry()
    get_key = module.get_secondary_item_key
    for row in rows:
        key = get_key(row)
        row.stock_qty -= flt(used_secondary_items.get(key))
        row.stock_qty = row.stock_qty * flt(self.fg_completed_qty) / flt(pending_qty)
        if used_secondary_items.get(key):
            used_secondary_items[key] -= row.stock_qty
        if cint(frappe.get_cached_value("UOM", row.stock_uom, "must_be_whole_number")):
            row.stock_qty = frappe.utils.ceil(row.stock_qty)
    return rows


def _representative_manufacture_rows(self):
    import frappe

    se = frappe.qb.DocType("Stock Entry")
    sed = frappe.qb.DocType("Stock Entry Detail")
    method_source = _source(_original_manufacture or self.__class__.get_items_from_manufacture_stock_entry)
    fields = [sed.item_code, sed.item_name, sed.description, sed.stock_uom, sed.is_finished_item]
    for fieldname in (
        "is_scrap_item",
        "secondary_item_type",
        "valuation_type",
        "bom_secondary_item",
        "batch_no",
        "serial_no",
        "use_serial_batch_fields",
        "s_warehouse",
        "t_warehouse",
        "bom_no",
    ):
        if f"SED.{fieldname}" in method_source:
            fields.append(getattr(sed, fieldname))
    lines = (
        frappe.qb.from_(sed)
        .join(se)
        .on(sed.parent == se.name)
        .select(*fields)
        .where((se.docstatus == 1) & (se.purpose == "Manufacture") & (se.work_order == self.work_order))
        .orderby(se.creation)
        .orderby(se.name)
        .orderby(sed.idx)
        .run(as_dict=True)
    )
    representative = {}
    for line in lines:
        representative.setdefault(line.item_code, line)
    return representative


def _compatible_manufacture(self):
    import frappe
    from frappe.query_builder.functions import Min, Sum

    se = frappe.qb.DocType("Stock Entry")
    sed = frappe.qb.DocType("Stock Entry Detail")
    query = frappe.qb.from_(sed).join(se).on(sed.parent == se.name).where(se.docstatus == 1)

    method_source = _source(_original_manufacture or self.__class__.get_items_from_manufacture_stock_entry)
    common = [
        sed.item_code,
        sed.item_name,
        sed.description,
        sed.stock_uom,
        sed.uom,
        sed.basic_rate,
        sed.conversion_factor,
        sed.is_finished_item,
    ]
    for fieldname in (
        "is_scrap_item",
        "secondary_item_type",
        "valuation_type",
        "bom_secondary_item",
        "batch_no",
        "serial_no",
        "use_serial_batch_fields",
        "s_warehouse",
        "t_warehouse",
        "bom_no",
    ):
        if f"SED.{fieldname}" in method_source:
            common.append(getattr(sed, fieldname))

    if self.source_stock_entry:
        return (
            query.select(sed.name, sed.qty, sed.transfer_qty, *common)
            .where(se.name == self.source_stock_entry)
            .orderby(sed.idx)
            .run(as_dict=True)
        )

    # Current ERPNext aggregates in stock UOM and uses a real row for metadata.
    rows = (
        query.select(
            sed.item_code,
            Sum(sed.transfer_qty).as_("qty"),
            Sum(sed.transfer_qty).as_("transfer_qty"),
            Sum(sed.basic_rate * sed.transfer_qty).as_("weighted_rate_total"),
        )
        .where(se.purpose == "Manufacture")
        .where(se.work_order == self.work_order)
        .groupby(sed.item_code)
        .orderby(Min(sed.idx))
        .run(as_dict=True)
    )
    representative = _representative_manufacture_rows(self)
    for row in rows:
        transfer_qty = row.transfer_qty or 0
        row.basic_rate = (row.weighted_rate_total or 0) / transfer_qty if transfer_qty else 0
        row.pop("weighted_rate_total", None)
        row.update(representative.get(row.item_code) or {})
        row.uom = row.stock_uom
        row.conversion_factor = 1
    return rows


def apply_manufacturing_grouping_patch():
    global _original_job_card, _original_manufacture, _patched_job_card, _patched_manufacture
    module = _load_stock_entry()
    if module is None:
        return False
    cls = module.StockEntry
    changed = False
    if _needs_job_card_patch(cls):
        _original_job_card = cls.get_secondary_items_from_job_card
        _patched_job_card = _compatible_job_card
        cls.get_secondary_items_from_job_card = _patched_job_card  # nosemgrep
        changed = True
    if _needs_manufacture_patch(cls):
        _original_manufacture = cls.get_items_from_manufacture_stock_entry
        _patched_manufacture = _compatible_manufacture
        cls.get_items_from_manufacture_stock_entry = _patched_manufacture  # nosemgrep
        changed = True
    return changed
