"""Backport ERPNext's PostgreSQL-safe previous-fiscal-year lookup."""

import inspect
from importlib import import_module

NAME = "erpnext_period_closing_fiscal_year"
UPSTREAM_COMMIT = "develop"
_patched = None



def _load_module():
    try:
        return import_module("erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher")
    except ImportError:
        return None


def is_applied():
    module = _load_module()
    cls = getattr(module, "PeriodClosingVoucher", None) if module else None
    return cls is not None and _patched is not None and cls.check_if_previous_year_closed is _patched


def is_needed():
    module = _load_module()
    cls = getattr(module, "PeriodClosingVoucher", None) if module else None
    method = getattr(cls, "check_if_previous_year_closed", None) if cls else None
    if method is None or is_applied():
        return False
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return "previous_fiscal_year[0][1]" in source


def apply():
    global _patched
    if not is_needed():
        return is_applied()

    from frappe import db
    from frappe.utils import add_days
    from erpnext.accounts import utils as accounts_utils
    pcv = _load_module()

    def patched(self):
        last_year_closing = add_days(self.fy_start_date, -1)
        previous_fiscal_year = accounts_utils.get_fiscal_year(
            last_year_closing, company=self.company, boolean=True
        )
        if not previous_fiscal_year:
            return

        # ERPNext develop fix: get_fiscal_year returns one
        # (name, start_date, end_date) tuple, not a list of tuples.
        previous_fiscal_year_start_date = previous_fiscal_year[1]
        previous_fiscal_year_closed = db.exists(
            "Period Closing Voucher",
            {
                "period_end_date": ("between", [previous_fiscal_year_start_date, last_year_closing]),
                "docstatus": 1,
                "company": self.company,
            },
        )
        if previous_fiscal_year_closed:
            return

        gle_exists_in_previous_year = db.exists(
            "GL Entry",
            {
                "posting_date": ("between", [previous_fiscal_year_start_date, last_year_closing]),
                "company": self.company,
                "is_cancelled": 0,
            },
        )
        if not gle_exists_in_previous_year:
            return

        from frappe import _
        from frappe import throw
        throw(_("Previous Year is not closed, please close it first"))

    _patched = patched
    pcv.PeriodClosingVoucher.check_if_previous_year_closed = patched  # nosemgrep
    return True
