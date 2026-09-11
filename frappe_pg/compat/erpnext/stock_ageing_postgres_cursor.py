"""Backport ERPNext's PostgreSQL-safe Stock Ageing cursor behavior."""

import inspect
from importlib import import_module

NAME = "erpnext_stock_ageing_postgres_cursor"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.stock.report.stock_ageing.stock_ageing")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _needs_patch(cls):
    method = getattr(cls, "generate", None)
    if method is None:
        return False
    source = _source(method)
    return (
        "with frappe.db.unbuffered_cursor():" in source
        and "stock_ledger_entries = self._get_stock_ledger_entries()" in source
        and 'frappe.db.db_type == "postgres"' not in source
    )


def _compatible_generate(self):
    import frappe

    # Keep upstream behavior unchanged when callers provide explicit SLE rows.
    if self.sle is not None or frappe.db.db_type != "postgres":
        return _original(self)

    stock_ledger_entries = None
    bundle_wise_serial_nos, bundle_wise_batch_nos = self._get_bundle_wise_details(stock_ledger_entries)

    self.prepare_stock_reco_voucher_wise_count()
    module = _load()
    self.float_precision = module.get_float_precision()

    # Version-16 already prefetches these because nested queries are unsafe while
    # streaming. PostgreSQL additionally cannot execute nested queries on the same
    # named cursor, so fully consume the SLE iterator on the normal cursor first.
    self._prefetch_batchwise_valuations()
    self._prefetch_valuation_methods()
    stock_ledger_entries = list(self._get_stock_ledger_entries())

    for row in stock_ledger_entries:
        self._process_stock_ledger_entry(row, bundle_wise_serial_nos, bundle_wise_batch_nos)

    self._recompute_moving_average_slots()
    self._rebalance_batch_slots()

    if not self.filters.get("show_warehouse_wise_stock"):
        self.item_details = self._aggregate_details_by_item(self.item_details)

    return self.item_details


def is_applied():
    module = _load()
    cls = getattr(module, "FIFOSlots", None) if module else None
    return cls is not None and _patched is not None and cls.generate is _patched


def is_needed():
    module = _load()
    cls = getattr(module, "FIFOSlots", None) if module else None
    return cls is not None and not is_applied() and _needs_patch(cls)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    cls = module.FIFOSlots
    _original = cls.generate
    _patched = _compatible_generate
    cls.generate = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    cls = getattr(module, "FIFOSlots", None) if module else None
    if cls is None or not is_applied():
        return False
    cls.generate = _original  # nosemgrep
    _original = None
    _patched = None
    return True
