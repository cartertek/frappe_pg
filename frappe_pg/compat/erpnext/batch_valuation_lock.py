"""ERPNext batch-valuation row-lock compatibility for PostgreSQL.

Older ERPNext releases place ``FOR UPDATE`` on a grouped Serial and Batch Entry
query. PostgreSQL rejects row locks on grouped queries. ERPNext develop fixes
this by locking the matching detail rows in a separate plain SELECT before
running the grouped aggregate. This adapter backports that behavior without
modifying ERPNext source.
"""

import inspect
from importlib import import_module

NAME = "erpnext_batch_valuation_lock"
UPSTREAM_COMMIT = "b85776c00b"

_original_get_batch_stock_before_date = None
_patched_get_batch_stock_before_date = None


def _load_serial_batch_bundle():
    try:
        return import_module("erpnext.stock.serial_batch_bundle")
    except ImportError:
        return None


def _upstream_is_compatible(method):
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return (
        'if frappe.db.db_type == "postgres"' in source
        and ".select(child.name).where(conditions).for_update().run()" in source
        and 'if frappe.db.db_type != "postgres"' in source
    )


def _legacy_shape_supported(method):
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return (
        '.for_update()' in source
        and '.groupby(child.batch_no)' in source
        and 'child.stock_value_difference' in source
        and 'child.type_of_transaction.isin(["Inward", "Outward"])' in source
    )


def is_needed():
    module = _load_serial_batch_bundle()
    cls = getattr(module, "BatchNoValuation", None) if module else None
    method = getattr(cls, "get_batch_stock_before_date", None) if cls else None
    return (
        method is not None
        and not is_applied()
        and _legacy_shape_supported(method)
        and not _upstream_is_compatible(method)
    )


def is_applied():
    module = _load_serial_batch_bundle()
    cls = getattr(module, "BatchNoValuation", None) if module else None
    return (
        cls is not None
        and _patched_get_batch_stock_before_date is not None
        and cls.get_batch_stock_before_date is _patched_get_batch_stock_before_date
    )


def _compatible_get_batch_stock_before_date(self):
    module = _load_serial_batch_bundle()
    frappe = module.frappe
    Sum = module.Sum
    ExistsCriterion = module.ExistsCriterion

    if not self.batchwise_valuation_batches:
        return []

    child = frappe.qb.DocType("Serial and Batch Entry")

    sle_creation = self.sle.creation if self.sle.get("name") else None
    if not self.sle.get("name") and self.sle.get("serial_and_batch_bundle"):
        sle_creation = frappe.db.get_value(
            "Stock Ledger Entry",
            {"serial_and_batch_bundle": self.sle.serial_and_batch_bundle, "is_cancelled": 0},
            "creation",
        )

    timestamp_condition = ""
    if self.sle.posting_datetime:
        timestamp_condition = child.posting_datetime < self.sle.posting_datetime

        sle_table = frappe.qb.DocType("Stock Ledger Entry")
        if sle_creation:
            tie_condition = ExistsCriterion(
                frappe.qb.from_(sle_table)
                .select(sle_table.name)
                .where(
                    (sle_table.serial_and_batch_bundle == child.parent)
                    & (sle_table.is_cancelled == 0)
                    & (sle_table.creation < sle_creation)
                )
            )
        else:
            tie_condition = ExistsCriterion(
                frappe.qb.from_(sle_table)
                .select(sle_table.name)
                .where((sle_table.serial_and_batch_bundle == child.parent) & (sle_table.is_cancelled == 0))
            )

        timestamp_condition |= (child.posting_datetime == self.sle.posting_datetime) & tie_condition

    conditions = (
        (child.item_code == self.sle.item_code)
        & (child.warehouse == self.sle.warehouse)
        & (child.batch_no.isin(self.batchwise_valuation_batches))
        & (child.docstatus == 1)
        & (child.is_cancelled == 0)
        & (child.type_of_transaction.isin(["Inward", "Outward"]))
    )

    if self.sle.voucher_detail_no:
        conditions &= child.voucher_detail_no != self.sle.voucher_detail_no
    elif self.sle.voucher_no:
        conditions &= child.voucher_no != self.sle.voucher_no

    conditions &= child.voucher_type != "Pick List"
    if timestamp_condition:
        conditions &= timestamp_condition
    if self.stock_closing_from_datetime:
        conditions &= child.posting_datetime >= self.stock_closing_from_datetime

    # Preserve the original locking semantics: lock the matching detail rows,
    # then aggregate those same rows without a lock clause on the GROUP BY.
    frappe.qb.from_(child).select(child.name).where(conditions).for_update().run()

    return (
        frappe.qb.from_(child)
        .select(
            child.batch_no,
            Sum(child.stock_value_difference).as_("incoming_rate"),
            Sum(child.qty).as_("qty"),
        )
        .where(conditions)
        .groupby(child.batch_no)
        .run(as_dict=True)
    )


def apply():
    global _original_get_batch_stock_before_date, _patched_get_batch_stock_before_date
    module = _load_serial_batch_bundle()
    cls = getattr(module, "BatchNoValuation", None) if module else None
    method = getattr(cls, "get_batch_stock_before_date", None) if cls else None
    if (
        method is None
        or is_applied()
        or _upstream_is_compatible(method)
        or not _legacy_shape_supported(method)
    ):
        return False

    _original_get_batch_stock_before_date = method
    _patched_get_batch_stock_before_date = _compatible_get_batch_stock_before_date
    cls.get_batch_stock_before_date = _compatible_get_batch_stock_before_date  # nosemgrep
    return True


def remove():
    global _original_get_batch_stock_before_date, _patched_get_batch_stock_before_date
    module = _load_serial_batch_bundle()
    cls = getattr(module, "BatchNoValuation", None) if module else None
    if cls is None or not is_applied():
        return False

    cls.get_batch_stock_before_date = _original_get_batch_stock_before_date  # nosemgrep
    _original_get_batch_stock_before_date = None
    _patched_get_batch_stock_before_date = None
    return True
