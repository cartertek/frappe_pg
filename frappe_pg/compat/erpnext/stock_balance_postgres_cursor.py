"""Make ERPNext Stock Balance cursor iteration safe on PostgreSQL."""

import inspect
from importlib import import_module

NAME = "erpnext_stock_balance_postgres_cursor"
_original = None
_patched = None
_method_name = None


def _load():
    try:
        return import_module("erpnext.stock.report.stock_balance.stock_balance")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _target(cls):
    for name in ("get_item_warehouse_map", "prepare_item_warehouse_map_for_current_period"):
        method = getattr(cls, name, None)
        source = _source(method) if method else ""
        if "with frappe.db.unbuffered_cursor():" in source and "as_iterator=True" in source:
            return name, method
    return None, None


def _compatible_v15(self):
    import frappe

    module = _load()
    if frappe.db.db_type != "postgres":
        return _original(self)

    item_warehouse_map = {}
    self.opening_vouchers = self.get_opening_vouchers()
    if self.filters.get("show_stock_ageing_data"):
        self.sle_entries = self.sle_query.run(as_dict=True)

    self.prepare_stock_reco_voucher_wise_count()
    frappe.get_cached_doc("System Settings")
    if not self.filters.get("show_stock_ageing_data"):
        self.sle_entries = self.sle_query.run(as_dict=True)

    for entry in self.sle_entries:
        group_by_key = self.get_group_by_key(entry)
        if group_by_key not in item_warehouse_map:
            self.initialize_data(item_warehouse_map, group_by_key, entry)
        self.prepare_item_warehouse_map(item_warehouse_map, entry, group_by_key)
        if self.opening_data.get(group_by_key):
            del self.opening_data[group_by_key]

    for group_by_key, entry in self.opening_data.items():
        if group_by_key not in item_warehouse_map:
            self.initialize_data(item_warehouse_map, group_by_key, entry)

    return module.filter_items_with_no_transactions(
        item_warehouse_map, self.float_precision, self.inventory_dimensions
    )


def _compatible_v16(self):
    import frappe

    module = _load()
    if frappe.db.db_type != "postgres":
        return _original(self)

    self.opening_vouchers = self.get_opening_vouchers()
    if self.filters.get("show_stock_ageing_data"):
        self.sle_entries = self.sle_query.run(as_dict=True)

    self.prepare_stock_reco_voucher_wise_count()
    frappe.get_cached_doc("System Settings")
    if not self.filters.get("show_stock_ageing_data"):
        self.sle_entries = self.sle_query.run(as_dict=True)

    for entry in self.sle_entries:
        group_by_key = self.get_group_by_key(entry)
        if group_by_key not in self.item_warehouse_map:
            self.initialize_data(group_by_key, entry)
        self.prepare_item_warehouse_map(entry, group_by_key)

    self.item_warehouse_map = module.filter_items_with_no_transactions(
        self.item_warehouse_map, self.float_precision, self.inventory_dimensions
    )


def is_applied():
    module = _load()
    cls = getattr(module, "StockBalanceReport", None) if module else None
    return (
        cls is not None
        and _method_name is not None
        and _patched is not None
        and getattr(cls, _method_name, None) is _patched
    )


def is_needed():
    module = _load()
    cls = getattr(module, "StockBalanceReport", None) if module else None
    if cls is None or is_applied():
        return False
    name, method = _target(cls)
    return name is not None and method is not None


def apply():
    global _original, _patched, _method_name
    if not is_needed():
        return False
    module = _load()
    cls = module.StockBalanceReport
    name, method = _target(cls)
    _method_name = name
    _original = method
    _patched = _compatible_v15 if name == "get_item_warehouse_map" else _compatible_v16
    setattr(cls, name, _patched)  # nosemgrep
    return True


def remove():
    global _original, _patched, _method_name
    module = _load()
    cls = getattr(module, "StockBalanceReport", None) if module else None
    if cls is None or not is_applied():
        return False
    setattr(cls, _method_name, _original)  # nosemgrep
    _original = None
    _patched = None
    _method_name = None
    return True
