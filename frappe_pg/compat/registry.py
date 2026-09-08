"""Registry for narrowly scoped application-level PostgreSQL compatibility overrides."""

from frappe_pg.compat.erpnext import trends_group_by
from frappe_pg.compat.frappe import (
    goal_aggregation,
    postgres_decimal_metadata,
    schema_type_conversion,
    unique_insert_transaction,
)

_COMPATIBILITY_OVERRIDES = (
    goal_aggregation,
    postgres_decimal_metadata,
    schema_type_conversion,
    unique_insert_transaction,
    trends_group_by,
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
