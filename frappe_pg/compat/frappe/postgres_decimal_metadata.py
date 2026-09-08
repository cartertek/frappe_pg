"""Backport PostgreSQL decimal precision metadata for older Frappe branches."""

import inspect

NAME = "frappe_postgres_decimal_metadata"

_original_get_table_columns_description = None
_patched_get_table_columns_description = None


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
        and _patched_get_table_columns_description is not None
        and database.get_table_columns_description is _patched_get_table_columns_description
    )


def is_needed():
    database = _load_postgres_database()
    return (
        database is not None
        and not is_applied()
        and not _upstream_reports_numeric_precision(database.get_table_columns_description)
    )


def _get_value(row, key):
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _set_value(row, key, value):
    if isinstance(row, dict):
        row[key] = value
    else:
        setattr(row, key, value)


def apply():
    """Add decimal precision/scale to PostgreSQL column descriptions when upstream omits it."""
    global _original_get_table_columns_description, _patched_get_table_columns_description

    database = _load_postgres_database()
    if (
        database is None
        or is_applied()
        or _upstream_reports_numeric_precision(database.get_table_columns_description)
    ):
        return False

    _original_get_table_columns_description = database.get_table_columns_description

    def compatible_get_table_columns_description(self, table_name):
        descriptions = _original_get_table_columns_description(self, table_name)
        numeric_columns = {
            _get_value(row, "name")
            for row in descriptions
            if str(_get_value(row, "type") or "").lower() == "numeric"
        }
        numeric_columns.discard(None)
        if not numeric_columns:
            return descriptions

        precision_rows = self.sql(
            """
            SELECT column_name AS name, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_name = %s
              AND data_type = 'numeric'
            """,
            table_name,
            as_dict=True,
        )
        precision_by_name = {_get_value(row, "name"): row for row in precision_rows}

        for row in descriptions:
            name = _get_value(row, "name")
            if name not in numeric_columns or name not in precision_by_name:
                continue
            precision = _get_value(precision_by_name[name], "numeric_precision")
            scale = _get_value(precision_by_name[name], "numeric_scale")
            if precision is not None and scale is not None:
                _set_value(row, "type", f"decimal({precision},{scale})")

        return descriptions

    _patched_get_table_columns_description = compatible_get_table_columns_description
    database.get_table_columns_description = compatible_get_table_columns_description  # nosemgrep
    return True


def remove():
    """Restore Frappe's original metadata implementation if this adapter installed itself."""
    global _original_get_table_columns_description, _patched_get_table_columns_description

    database = _load_postgres_database()
    if database is None or not is_applied():
        return False

    database.get_table_columns_description = _original_get_table_columns_description  # nosemgrep
    _original_get_table_columns_description = None
    _patched_get_table_columns_description = None
    return True
