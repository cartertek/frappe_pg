"""Backport ERPNext's PostgreSQL-safe future-stock-voucher locking semantics."""

import inspect
from datetime import datetime, time, timedelta
from importlib import import_module

NAME = "erpnext_future_stock_voucher_lock"
UPSTREAM_COMMIT = "develop"

_original = None
_patched = None


def _load_module():
    try:
        return import_module("erpnext.accounts.utils")
    except ImportError:
        return None


def _upstream_is_compatible(method):
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return (
        'if frappe.db.db_type == "postgres"' in source
        and "for_update().run()" in source
        and ".groupby(SLE.voucher_type, SLE.voucher_no)" in source
    )


def _legacy_shape_supported(method):
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return (
        "get_future_stock_vouchers" in source
        and "voucher_type" in source
        and "voucher_no" in source
        and "for update" in source.lower()
    ) or ("get_future_stock_vouchers" in source and ".distinct()" in source and ".for_update()" in source)


def is_applied():
    module = _load_module()
    return module is not None and _patched is not None and module.get_future_stock_vouchers is _patched


def is_needed():
    module = _load_module()
    method = getattr(module, "get_future_stock_vouchers", None) if module else None
    return (
        method is not None
        and not is_applied()
        and _legacy_shape_supported(method)
        and not _upstream_is_compatible(method)
    )


def _time_parameter(value):
    """Convert MariaDB-style TIME timedelta values to a PostgreSQL time value."""
    if not isinstance(value, timedelta):
        return value
    total_microseconds = int(value.total_seconds() * 1_000_000) % (24 * 60 * 60 * 1_000_000)
    hours, remainder = divmod(total_microseconds, 60 * 60 * 1_000_000)
    minutes, remainder = divmod(remainder, 60 * 1_000_000)
    seconds, microseconds = divmod(remainder, 1_000_000)
    return time(hours, minutes, seconds, microseconds)


def _compatible_get_future_stock_vouchers(
    posting_date, posting_time, for_warehouses=None, for_items=None, company=None
):
    import frappe
    from frappe.query_builder.custom import ConstantColumn
    from frappe.query_builder.functions import Min
    from frappe.utils import getdate

    posting_time = _time_parameter(posting_time)

    SLE = frappe.qb.DocType("Stock Ledger Entry")
    posting_datetime = SLE.posting_date + SLE.posting_time
    threshold = datetime.combine(getdate(posting_date), posting_time)

    conditions = (posting_datetime >= threshold) & (SLE.is_cancelled == 0)
    if for_items:
        conditions &= SLE.item_code.isin(for_items)
    if for_warehouses:
        conditions &= SLE.warehouse.isin(for_warehouses)
    if company:
        conditions &= SLE.company == company

    # ERPNext develop locks the matching SLE rows separately because PostgreSQL
    # rejects FOR UPDATE on DISTINCT/GROUP BY queries. These locks remain held
    # for the surrounding transaction, preserving repost concurrency semantics.
    frappe.qb.from_(SLE).select(ConstantColumn(1)).where(conditions).for_update().run()

    # Grouping preserves one result per voucher while MIN() retains the old
    # chronological ordering without expanding the DISTINCT key.
    future_stock_vouchers = (
        frappe.qb.from_(SLE)
        .select(SLE.voucher_type, SLE.voucher_no)
        .where(conditions)
        .groupby(SLE.voucher_type, SLE.voucher_no)
        .orderby(Min(posting_datetime))
        .orderby(Min(SLE.creation))
        .run(as_dict=True)
    )
    return [(d.voucher_type, d.voucher_no) for d in future_stock_vouchers]


def apply():
    global _original, _patched
    module = _load_module()
    method = getattr(module, "get_future_stock_vouchers", None) if module else None
    if (
        method is None
        or is_applied()
        or _upstream_is_compatible(method)
        or not _legacy_shape_supported(method)
    ):
        return False

    _original = method
    _patched = _compatible_get_future_stock_vouchers
    module.get_future_stock_vouchers = _patched  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load_module()
    if module is None or not is_applied():
        return False
    module.get_future_stock_vouchers = _original  # nosemgrep
    _original = None
    _patched = None
    return True
