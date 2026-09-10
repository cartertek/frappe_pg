"""Registry for narrowly scoped application-level PostgreSQL compatibility overrides."""

from frappe_pg.compat.erpnext import (
    batch_valuation_advisory_lock,
    batch_valuation_lock,
    bom_stock_analysis_grouping,
    future_stock_voucher_lock,
    manufacturing_grouping,
    payment_ledger_grouping,
    payment_terms_status,
    period_closing_fiscal_year,
    pick_list_lock,
    stock_reservation_grouping,
    stock_reservation_lock,
    trends_group_by,
)
from frappe_pg.compat.frappe import (
    goal_aggregation,
    postgres_automatic_index_drop,
    postgres_boolean_values,
    postgres_date_functions,
    postgres_decimal_metadata,
    postgres_unique_violation,
    schema_type_conversion,
    unique_insert_transaction,
)

_COMPATIBILITY_OVERRIDES = (
    goal_aggregation,
    postgres_automatic_index_drop,
    postgres_boolean_values,
    postgres_date_functions,
    postgres_decimal_metadata,
    postgres_unique_violation,
    schema_type_conversion,
    unique_insert_transaction,
    trends_group_by,
    payment_ledger_grouping,
    payment_terms_status,
    period_closing_fiscal_year,
    batch_valuation_lock,
    batch_valuation_advisory_lock,
    future_stock_voucher_lock,
    manufacturing_grouping,
    bom_stock_analysis_grouping,
    stock_reservation_grouping,
    pick_list_lock,
    stock_reservation_lock,
)


def apply_compatibility_overrides():
    """Apply every compatibility override that is needed by the installed apps."""
    results = {}
    for override in _COMPATIBILITY_OVERRIDES:
        results[override.NAME] = override.apply()
    return results


def get_compatibility_status():
    """Return runtime status for all registered compatibility overrides."""
    return {
        override.NAME: {
            "needed": override.is_needed(),
            "applied": override.is_applied(),
        }
        for override in _COMPATIBILITY_OVERRIDES
    }
