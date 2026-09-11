"""Use a buffered GL query for Period Closing Voucher on PostgreSQL."""

import inspect
from importlib import import_module

NAME = "erpnext_period_closing_postgres_cursor"
_original = None
_patched = None


def _load():
    try:
        return import_module("erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher")
    except ImportError:
        return None


def _source(method):
    try:
        return inspect.getsource(method)
    except (OSError, TypeError):
        return ""


def _needs_patch(cls):
    method = getattr(cls, "get_account_balances_based_on_dimensions", None)
    if method is None:
        return False
    source = _source(method)
    return "with frappe.db.unbuffered_cursor():" in source and "as_iterator=True" in source


def _compatible(self, report_type):
    import frappe

    if frappe.db.db_type != "postgres":
        return _original(self, report_type)

    self.get_accounting_dimension_fields()
    acc_bal_dict = frappe._dict()

    # Frappe v15's PostgreSQL backend does not implement unbuffered_cursor().
    # Buffer the same rows first, then apply the unchanged aggregation logic.
    gl_entries = self.get_gl_entries_for_current_period(report_type, as_iterator=False)
    for gle in gl_entries:
        acc_bal_dict = self.set_account_balance_dict(gle, acc_bal_dict)

    if report_type == "Balance Sheet" and self.is_first_period_closing_voucher():
        opening_entries = self.get_gl_entries_for_current_period(report_type, only_opening_entries=True)
        for gle in opening_entries:
            acc_bal_dict = self.set_account_balance_dict(gle, acc_bal_dict)

    return acc_bal_dict


def is_applied():
    module = _load()
    cls = getattr(module, "PeriodClosingVoucher", None) if module else None
    return (
        cls is not None and _patched is not None and cls.get_account_balances_based_on_dimensions is _patched
    )


def is_needed():
    module = _load()
    cls = getattr(module, "PeriodClosingVoucher", None) if module else None
    return cls is not None and not is_applied() and _needs_patch(cls)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    cls = module.PeriodClosingVoucher
    _original = cls.get_account_balances_based_on_dimensions
    _patched = _compatible
    cls.get_account_balances_based_on_dimensions = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    cls = getattr(module, "PeriodClosingVoucher", None) if module else None
    if cls is None or not is_applied():
        return False
    cls.get_account_balances_based_on_dimensions = _original  # nosemgrep
    _original = None
    _patched = None
    return True
