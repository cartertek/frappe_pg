"""Backport ERPNext's PostgreSQL-safe Pick List grouped locking."""


def apply_pick_list_locking_patch():
    import inspect

    import frappe
    from erpnext.stock.doctype.pick_list import pick_list
    from frappe.query_builder.functions import Max, Sum

    original = pick_list.get_picked_items_qty
    if getattr(original, "_frappe_pg_pick_list_locking_fix", False):
        return
    try:
        source = inspect.getsource(original)
    except (OSError, TypeError):
        return
    if ".for_update()" not in source or ("GROUP" in source and 'db_type == "postgres"' in source):
        return

    def compatible(items, contains_packed_items=False):
        if frappe.db.db_type != "postgres":
            return original(items, contains_packed_items)

        pi_item = frappe.qb.DocType("Pick List Item")
        group_field = pi_item.product_bundle_item if contains_packed_items else pi_item.sales_order_item
        conditions = (pi_item.docstatus == 1) & group_field.isin(items)
        query = (
            frappe.qb.from_(pi_item)
            .select(
                Max(pi_item.sales_order_item).as_("sales_order_item"),
                Max(pi_item.product_bundle_item).as_("product_bundle_item"),
                Max(pi_item.item_code).as_("item_code"),
                pi_item.sales_order,
                Sum(pi_item.stock_qty).as_("stock_qty"),
                Sum(pi_item.picked_qty).as_("picked_qty"),
            )
            .where(conditions)
            .groupby(group_field, pi_item.sales_order)
        )
        # PostgreSQL cannot lock a grouped aggregate. Lock the same underlying
        # detail rows first; row locks remain held for the surrounding transaction.
        frappe.qb.from_(pi_item).select(pi_item.name).where(conditions).for_update().run()
        return query.run(as_dict=True)

    compatible._frappe_pg_pick_list_locking_fix = True
    pick_list.get_picked_items_qty = compatible  # nosemgrep
