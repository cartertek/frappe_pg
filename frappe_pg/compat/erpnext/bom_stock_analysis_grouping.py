"""Backport PostgreSQL-safe ERPNext BOM Stock Analysis grouping semantics."""

import inspect
from importlib import import_module

NAME = "erpnext_bom_stock_analysis_grouping"
_original_bom_data = None
_original_producible = None
_patched_bom_data = None
_patched_producible = None


def _load():
    try:
        return import_module("erpnext.manufacturing.report.bom_stock_analysis.bom_stock_analysis")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _needs_bom_data_patch(method):
    source = _source(method)
    return (
        ".groupby(bom_item.item_code)" in source
        and 'bom_item.parent.as_("from_bom_no")' in source
        and 'Max(bom_item.parent).as_("from_bom_no")' not in source
    )


def _needs_producible_patch(method):
    source = _source(method)
    return (
        ".groupby(BOM_ITEM.item_code)" in source
        and 'BOM_ITEM.parent.as_("from_bom_no")' in source
        and 'Max(BOM_ITEM.parent).as_("from_bom_no")' not in source
    )


def _stock_qty_by_item(filters):
    import frappe
    from frappe.query_builder.functions import Sum
    from pypika.terms import ExistsCriterion

    bin_table = frappe.qb.DocType("Bin")
    query = (
        frappe.qb.from_(bin_table)
        .select(bin_table.item_code, Sum(bin_table.actual_qty).as_("actual_qty"))
        .groupby(bin_table.item_code)
    )
    if filters.get("warehouse"):
        warehouse_details = frappe.db.get_value(
            "Warehouse", filters.get("warehouse"), ["lft", "rgt"], as_dict=1
        )
        if warehouse_details:
            wh = frappe.qb.DocType("Warehouse")
            query = query.where(
                ExistsCriterion(
                    frappe.qb.from_(wh)
                    .select(wh.name)
                    .where(
                        (wh.lft >= warehouse_details.lft)
                        & (wh.rgt <= warehouse_details.rgt)
                        & (bin_table.warehouse == wh.name)
                    )
                )
            )
        else:
            query = query.where(bin_table.warehouse == filters.get("warehouse"))
    return query


def _representative_lines(doctype, bom, include_bom_fields=False):
    import frappe

    fields = ["item_code", "description"]
    if include_bom_fields:
        fields += ["bom_no", "is_phantom_item"]
    representative = {}
    for line in frappe.get_all(
        doctype,
        filters={"parent": bom, "parenttype": "BOM"},
        fields=fields,
        order_by="idx",
    ):
        existing = representative.get(line.item_code)
        if existing is None or (
            include_bom_fields and line.get("is_phantom_item") and not existing.get("is_phantom_item")
        ):
            representative[line.item_code] = line
    return representative


def _compatible_bom_data(filters):
    import frappe
    from frappe.query_builder.functions import IfNull, Max, Min, Sum

    module = _load()
    bom_item_table = "BOM Explosion Item" if filters.get("show_exploded_view") else "BOM Item"
    bom_item = frappe.qb.DocType(bom_item_table)
    stock_qty = _stock_qty_by_item(filters).as_("stock_qty")
    base = frappe.qb.from_(bom_item)
    base = base.join(stock_qty) if filters.get("warehouse") else base.left_join(stock_qty)
    query = (
        base.on(bom_item.item_code == stock_qty.item_code)
        .select(
            bom_item.item_code,
            Max(bom_item.parent).as_("from_bom_no"),
            Sum(bom_item.qty_consumed_per_unit).as_("qty_per_unit"),
            IfNull(Max(stock_qty.actual_qty), 0).as_("actual_qty"),
        )
        .where((bom_item.parent == filters.get("bom")) & (bom_item.parenttype == "BOM"))
        .groupby(bom_item.item_code)
        .orderby(Min(bom_item.idx))
    )
    data = query.run(as_dict=True)
    include_bom_fields = bom_item_table == "BOM Item"
    representative = _representative_lines(bom_item_table, filters.get("bom"), include_bom_fields)
    for row in data:
        line = representative.get(row.item_code)
        row.description = line.description if line else None
        if include_bom_fields:
            row.bom_no = line.bom_no if line else None
            row.is_phantom_item = line.is_phantom_item if line else None
    return module.explode_phantom_boms(data, filters) if include_bom_fields else data


def _legacy_producible_rows(rows, representative):
    """Preserve the positional row contract used by ERPNext v16 callers."""
    result = []
    for row in rows:
        line = representative.get(row.item_code)
        result.append(
            [
                row.item_code,
                line.description if line else None,
                row.from_bom_no,
                row.qty_per_unit,
                row.available_qty,
                row.producible_qty,
            ]
        )
    return result


def _compatible_producible(filters):
    import frappe
    from frappe import _
    from frappe.query_builder.functions import Floor, IfNull, Max, Min, Sum

    bom_item = frappe.qb.DocType("BOM Item")
    bom = frappe.qb.DocType("BOM")
    bin_table = frappe.qb.DocType("Bin")
    wh = frappe.qb.DocType("Warehouse")
    warehouse = filters.get("warehouse")
    if not warehouse:
        frappe.throw(_("Warehouse is required to get producible FG Items"))
    details = frappe.db.get_value("Warehouse", warehouse, ["lft", "rgt"], as_dict=1)
    if details:
        stock = (
            frappe.qb.from_(bin_table)
            .join(wh)
            .on(bin_table.warehouse == wh.name)
            .select(bin_table.item_code, Sum(bin_table.actual_qty).as_("actual_qty"))
            .where((wh.lft >= details.lft) & (wh.rgt <= details.rgt))
            .groupby(bin_table.item_code)
        )
    else:
        stock = (
            frappe.qb.from_(bin_table)
            .select(bin_table.item_code, Sum(bin_table.actual_qty).as_("actual_qty"))
            .where(bin_table.warehouse == warehouse)
            .groupby(bin_table.item_code)
        )
    query = (
        frappe.qb.from_(bom_item)
        .join(bom)
        .on(bom_item.parent == bom.name)
        .left_join(stock)
        .on(bom_item.item_code == stock.item_code)
        .select(
            bom_item.item_code,
            Max(bom_item.parent).as_("from_bom_no"),
            Max(bom_item.stock_qty / bom.quantity).as_("qty_per_unit"),
            Max(IfNull(stock.actual_qty, 0)).as_("available_qty"),
            Floor(Max(stock.actual_qty) / (Sum(bom_item.stock_qty) / Max(bom.quantity))).as_(
                "producible_qty"
            ),
        )
        .where((bom_item.parent == filters.get("bom")) & (bom_item.parenttype == "BOM"))
        .groupby(bom_item.item_code)
        .orderby(Min(bom_item.idx))
    )
    rows = query.run(as_dict=True)
    representative = _representative_lines("BOM Item", filters.get("bom"))

    # The legacy v16 caller indexes these rows positionally. ERPNext develop
    # changed both this producer and its caller to dict rows in the same patch;
    # backport only the PostgreSQL-safe query semantics here while preserving
    # the installed caller's six-column list contract.
    return _legacy_producible_rows(rows, representative)


def is_applied():
    module = _load()
    if module is None:
        return False
    bom_ok = _patched_bom_data is None or module.get_bom_data is _patched_bom_data
    prod_ok = _patched_producible is None or module.get_producible_fg_items is _patched_producible
    return (_patched_bom_data is not None or _patched_producible is not None) and bom_ok and prod_ok


def is_needed():
    module = _load()
    return module is not None and (
        _needs_bom_data_patch(module.get_bom_data) or _needs_producible_patch(module.get_producible_fg_items)
    )


def apply():
    global _original_bom_data, _original_producible, _patched_bom_data, _patched_producible
    module = _load()
    if module is None:
        return False
    changed = False
    if _needs_bom_data_patch(module.get_bom_data):
        _original_bom_data = module.get_bom_data
        _patched_bom_data = _compatible_bom_data
        module.get_bom_data = _patched_bom_data  # nosemgrep
        changed = True
    if _needs_producible_patch(module.get_producible_fg_items):
        _original_producible = module.get_producible_fg_items
        _patched_producible = _compatible_producible
        module.get_producible_fg_items = _patched_producible  # nosemgrep
        changed = True
    return changed


def remove():
    global _original_bom_data, _original_producible, _patched_bom_data, _patched_producible
    module = _load()
    if module is None:
        return False
    changed = False
    if _patched_bom_data is not None and module.get_bom_data is _patched_bom_data:
        module.get_bom_data = _original_bom_data  # nosemgrep
        changed = True
    if _patched_producible is not None and module.get_producible_fg_items is _patched_producible:
        module.get_producible_fg_items = _original_producible  # nosemgrep
        changed = True
    _original_bom_data = None
    _original_producible = None
    _patched_bom_data = None
    _patched_producible = None
    return changed
