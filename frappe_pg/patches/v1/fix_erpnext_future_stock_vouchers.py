"""Backport ERPNext's PostgreSQL-safe future stock voucher locking query."""


def apply_future_stock_vouchers_patch():
    import frappe
    from erpnext.accounts import utils as accounts_utils

    original = accounts_utils.get_future_stock_vouchers
    if getattr(original, "_frappe_pg_future_stock_vouchers_fix", False):
        return

    def patched(posting_date, posting_time, for_warehouses=None, for_items=None, company=None):
        if frappe.db.db_type != "postgres":
            return original(posting_date, posting_time, for_warehouses, for_items, company)

        values = [posting_date, posting_time]
        conditions = []
        if for_items:
            conditions.append("item_code in ({})".format(", ".join(["%s"] * len(for_items))))
            values.extend(for_items)
        if for_warehouses:
            conditions.append("warehouse in ({})".format(", ".join(["%s"] * len(for_warehouses))))
            values.extend(for_warehouses)
        if company:
            conditions.append("company = %s")
            values.append(company)

        extra_conditions = ""
        if conditions:
            extra_conditions = " and " + " and ".join(conditions)

        # Match ERPNext develop's PostgreSQL strategy: lock all matching SLE rows
        # in a separate statement, because PostgreSQL rejects FOR UPDATE together
        # with DISTINCT/GROUP BY. The locks survive until the surrounding
        # transaction ends, preserving the repost concurrency guarantee.
        where_clause = f"""
            (posting_date + posting_time) >= (CAST(%s AS date) + CAST(%s AS time))
            and is_cancelled = 0
            {extra_conditions}
        """
        frappe.db.sql(
            f'SELECT 1 FROM "tabStock Ledger Entry" WHERE {where_clause} FOR UPDATE',
            tuple(values),
        )

        # Develop replaced DISTINCT + ordering by non-selected columns with a
        # grouped query ordered by the earliest SLE timestamp/creation for each
        # voucher. That preserves voucher ordering while remaining PostgreSQL-valid.
        future_stock_vouchers = frappe.db.sql(
            f'''SELECT voucher_type, voucher_no
                FROM "tabStock Ledger Entry"
                WHERE {where_clause}
                GROUP BY voucher_type, voucher_no
                ORDER BY MIN(posting_date + posting_time) ASC, MIN(creation) ASC''',
            tuple(values),
            as_dict=True,
        )
        return [(d.voucher_type, d.voucher_no) for d in future_stock_vouchers]

    patched._frappe_pg_future_stock_vouchers_fix = True
    accounts_utils.get_future_stock_vouchers = patched  # nosemgrep
