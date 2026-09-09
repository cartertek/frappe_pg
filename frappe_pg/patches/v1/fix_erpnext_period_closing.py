"""Backport ERPNext's PostgreSQL-safe previous-fiscal-year lookup."""


def apply_period_closing_patch():
    from frappe import db
    from frappe.utils import add_days
    from erpnext.accounts import utils as accounts_utils
    from erpnext.accounts.doctype.period_closing_voucher import period_closing_voucher as pcv

    original = pcv.PeriodClosingVoucher.check_if_previous_year_closed
    if getattr(original, "_frappe_pg_period_closing_fix", False):
        return

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

    patched._frappe_pg_period_closing_fix = True
    pcv.PeriodClosingVoucher.check_if_previous_year_closed = patched  # nosemgrep
