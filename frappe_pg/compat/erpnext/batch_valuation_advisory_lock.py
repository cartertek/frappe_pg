"""ERPNext PostgreSQL transaction advisory lock for outgoing batch valuation."""

import inspect
from importlib import import_module

NAME = "erpnext_batch_valuation_advisory_lock"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.stock.serial_batch_bundle")
    except ImportError:
        return None


def is_applied():
    module = _load()
    cls = getattr(module, "BatchNoValuation", None) if module else None
    return cls is not None and _patched is not None and cls.calculate_avg_rate is _patched


def is_needed():
    module = _load()
    cls = getattr(module, "BatchNoValuation", None) if module else None
    method = getattr(cls, "calculate_avg_rate", None) if cls else None
    if method is None or is_applied():
        return False
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return "calculate_avg_rate_from_deprecarated_ledgers" in source and "transaction_advisory_lock" not in source


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    cls = module.BatchNoValuation
    _original = cls.calculate_avg_rate

    def compatible(self):
        frappe = module.frappe
        if (
            frappe.db.db_type == "postgres"
            and module.flt(self.sle.actual_qty) <= 0
            and hasattr(frappe.db, "transaction_advisory_lock")
        ):
            frappe.db.transaction_advisory_lock(("batch-valuation", self.sle.item_code, self.sle.warehouse))
        return _original(self)

    _patched = compatible
    cls.calculate_avg_rate = compatible  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    cls = getattr(module, "BatchNoValuation", None) if module else None
    if cls is None or not is_applied():
        return False
    cls.calculate_avg_rate = _original  # nosemgrep
    _original = None
    _patched = None
    return True
