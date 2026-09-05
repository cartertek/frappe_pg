"""Backward-compatible imports for the original frappe_pg patch module.

The active implementation lives under :mod:`frappe_pg.postgres`. Keeping this
module as a facade avoids a second, independent monkey-patch path.
"""

from frappe_pg.postgres.database_patches import (
    after_migrate,
    apply_postgres_fixes,
    check_patches_status,
    on_session_creation,
    patched_transform_query,
    remove_postgres_fixes,
)
from frappe_pg.postgres.db_functions import create_missing_functions
from frappe_pg.postgres.query_transformers import (
    apply_all_query_transformations,
    convert_date_format,
    convert_if_to_case,
    convert_ifnull_to_coalesce,
    remove_index_hints,
    split_by_comma,
)

__all__ = [
    "after_migrate",
    "apply_all_query_transformations",
    "apply_postgres_fixes",
    "check_patches_status",
    "convert_date_format",
    "convert_if_to_case",
    "convert_ifnull_to_coalesce",
    "create_missing_functions",
    "on_session_creation",
    "patched_transform_query",
    "remove_index_hints",
    "remove_postgres_fixes",
    "split_by_comma",
]
