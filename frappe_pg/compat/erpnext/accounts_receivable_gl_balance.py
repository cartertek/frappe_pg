"""Keep legacy Accounts Receivable grouped results at their requested two-column shape."""

import inspect
from importlib import import_module

NAME = "erpnext_accounts_receivable_gl_balance"
_original = None
_patched = None


def _load():
    try:
        return import_module(
            "erpnext.accounts.report.accounts_receivable_summary.accounts_receivable_summary"
        )
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
        'balance_calc_fields = ["party", "SUM(' in source
        and 'group_by="party"' in source
        and 'order_by="party"' not in source
        and "as_list=1" in source
    )


def _compatible(report_date, company, account_type):
    import frappe

    if account_type == "Payable":
        balance_calc_fields = ["party", "SUM(credit - debit) AS balance"]
    else:
        balance_calc_fields = ["party", "SUM(debit - credit) AS balance"]

    # Legacy Frappe appends its implicit ORDER BY modified to grouped SELECTs as
    # MAX(modified), which turns this requested 2-column as_list result into 3.
    # Order on the actual group key instead so no helper projection is injected.
    return frappe._dict(
        frappe.db.get_all(
            "GL Entry",
            fields=balance_calc_fields,
            filters={"posting_date": ("<=", report_date), "is_cancelled": 0, "company": company},
            group_by="party",
            order_by="party",
            as_list=1,
        )
    )


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_gl_balance is _patched


def is_needed():
    module = _load()
    method = getattr(module, "get_gl_balance", None) if module else None
    return method is not None and not is_applied() and _needs_patch(method)


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    _original = module.get_gl_balance
    _patched = _compatible
    module.get_gl_balance = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_gl_balance = _original  # nosemgrep
    _original = None
    _patched = None
    return True
