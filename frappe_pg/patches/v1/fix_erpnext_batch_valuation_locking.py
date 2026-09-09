"""Backport ERPNext's PostgreSQL batch-valuation advisory locking."""


def apply_batch_valuation_locking_patch():
    import frappe
    from frappe.utils import flt
    from erpnext.stock import serial_batch_bundle

    cls = serial_batch_bundle.BatchNoValuation
    original = cls.calculate_avg_rate
    if getattr(original, "_frappe_pg_batch_valuation_lock", False):
        return

    def compatible_calculate_avg_rate(self):
        # Current ERPNext serializes outgoing batch valuation by item/warehouse
        # on PostgreSQL because grouped history reads cannot use FOR UPDATE.
        if (
            frappe.db.db_type == "postgres"
            and flt(self.sle.actual_qty) <= 0
            and hasattr(frappe.db, "transaction_advisory_lock")
        ):
            frappe.db.transaction_advisory_lock(("batch-valuation", self.sle.item_code, self.sle.warehouse))
        return original(self)

    compatible_calculate_avg_rate._frappe_pg_batch_valuation_lock = True
    cls.calculate_avg_rate = compatible_calculate_avg_rate  # nosemgrep
