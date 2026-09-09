"""Backport PostgreSQL-safe Stock Reservation aggregate locking."""


def apply_stock_reservation_locking_patch():
    import inspect

    import frappe
    from frappe.query_builder.functions import Sum
    from erpnext.stock.doctype.stock_reservation_entry import stock_reservation_entry as sre_module

    original = sre_module.get_available_qty_to_reserve
    if getattr(original, "_frappe_pg_stock_reservation_lock", False):
        return

    source = inspect.getsource(original)
    uses_extended_qty = "transferred_qty" in source and "consumed_qty" in source
    uses_status_filter = ".status.notin" in source

    def patched(item_code: str, warehouse: str, batch_no: str | None = None, ignore_sre=None) -> float:
        if frappe.db.db_type != "postgres":
            return original(item_code, warehouse, batch_no, ignore_sre)

        from erpnext.stock.doctype.batch.batch import get_batch_qty

        if batch_no:
            return get_batch_qty(
                item_code=item_code,
                warehouse=warehouse,
                batch_no=batch_no,
                ignore_voucher_nos=[ignore_sre],
            )

        available_qty = sre_module.get_stock_balance(item_code, warehouse)
        if not available_qty:
            return available_qty

        sre = frappe.qb.DocType("Stock Reservation Entry")
        conditions = (
            (sre.docstatus == 1)
            & (sre.item_code == item_code)
            & (sre.warehouse == warehouse)
            & (sre.delivered_qty < sre.reserved_qty)
        )
        if uses_status_filter:
            conditions &= sre.status.notin(["Delivered", "Cancelled"])
        if ignore_sre:
            conditions &= sre.name != ignore_sre

        # PostgreSQL forbids FOR UPDATE on aggregate queries. Serialize the
        # reservation calculation on the Bin row (also covers the no-SRE case),
        # then lock matching SRE rows individually for the surrounding txn.
        bin_table = frappe.qb.DocType("Bin")
        (
            frappe.qb.from_(bin_table)
            .select(bin_table.name)
            .where((bin_table.item_code == item_code) & (bin_table.warehouse == warehouse))
            .for_update()
            .run()
        )
        frappe.qb.from_(sre).select(sre.name).where(conditions).orderby(sre.name).for_update().run()

        if uses_extended_qty:
            reserved_expr = sre.reserved_qty - sre.delivered_qty - sre.transferred_qty - sre.consumed_qty
        else:
            reserved_expr = sre.reserved_qty - sre.delivered_qty
        reserved_qty = frappe.qb.from_(sre).select(Sum(reserved_expr)).where(conditions).run()[0][0] or 0.0
        return available_qty - reserved_qty if reserved_qty else available_qty

    patched._frappe_pg_stock_reservation_lock = True
    sre_module.get_available_qty_to_reserve = patched  # nosemgrep
