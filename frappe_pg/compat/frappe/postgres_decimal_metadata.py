"""Backport PostgreSQL decimal precision metadata for older Frappe branches."""

import inspect

NAME = "frappe_postgres_decimal_metadata"

_original_get_column_type = None
_patched_get_column_type = None


def _load_postgres_database():
    try:
        from frappe.database.postgres.database import PostgresDatabase
    except ImportError:
        return None
    return PostgresDatabase


def _upstream_reports_numeric_precision(function):
    """Detect native precision/scale reporting by capability rather than version."""
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    return "numeric_precision" in source and "numeric_scale" in source


def is_applied():
    database = _load_postgres_database()
    return (
        database is not None
        and _patched_get_column_type is not None
        and database.get_column_type is _patched_get_column_type
    )


def is_needed():
    database = _load_postgres_database()
    return (
        database is not None
        and not is_applied()
        and not _upstream_reports_numeric_precision(database.get_column_type)
    )


def apply():
    """Return decimal precision/scale from PostgreSQL when older Frappe only returns ``numeric``."""
    global _original_get_column_type, _patched_get_column_type

    database = _load_postgres_database()
    if database is None or is_applied() or _upstream_reports_numeric_precision(database.get_column_type):
        return False

    _original_get_column_type = database.get_column_type

    def compatible_get_column_type(self, doctype, column):
        column_type = _original_get_column_type(self, doctype, column)
        if str(column_type).lower() != "numeric":
            return column_type

        from frappe.utils import get_table_name

        rows = self.sql(
            """
            SELECT numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s AND data_type = 'numeric'
            """,
            (get_table_name(doctype), column),
            as_dict=True,
        )
        if not rows:
            return column_type
        row = rows[0]
        precision = row.get("numeric_precision") if isinstance(row, dict) else row.numeric_precision
        scale = row.get("numeric_scale") if isinstance(row, dict) else row.numeric_scale
        if precision is None or scale is None:
            return column_type
        return f"decimal({precision},{scale})"

    _patched_get_column_type = compatible_get_column_type
    database.get_column_type = compatible_get_column_type  # nosemgrep
    return True


def remove():
    """Restore Frappe's original implementation if this adapter installed itself."""
    global _original_get_column_type, _patched_get_column_type

    database = _load_postgres_database()
    if database is None or not is_applied():
        return False

    database.get_column_type = _original_get_column_type  # nosemgrep
    _original_get_column_type = None
    _patched_get_column_type = None
    return True
