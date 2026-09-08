"""Backport Frappe's PostgreSQL incompatible-field-value handling.

Newer Frappe PostgreSQL schema code translates cast failures raised while
changing a field type into Frappe's normal incompatible-values validation
error. Older supported branches let the psycopg2 exception escape instead.
"""

import inspect

NAME = "frappe_schema_type_conversion"

_INCOMPATIBLE_TYPE_PGCODES = {"42804", "22P02", "22003"}
_original_alter = None
_patched_alter = None


def _load_postgres_table():
    try:
        from frappe.database.postgres.schema import PostgresTable
    except ImportError:
        return None
    return PostgresTable


def _upstream_handles_incompatible_values(function):
    """Detect Frappe's native incompatible-value handling by capability."""
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    return "is_data_truncated" in source and "Incompatible Values" in source


def is_applied():
    table = _load_postgres_table()
    return table is not None and _patched_alter is not None and table.alter is _patched_alter


def is_needed():
    table = _load_postgres_table()
    return table is not None and not is_applied() and not _upstream_handles_incompatible_values(table.alter)


def _is_incompatible_type_error(exc):
    return getattr(exc, "pgcode", None) in _INCOMPATIBLE_TYPE_PGCODES


def apply():
    """Translate PostgreSQL cast failures only while upstream lacks that behavior."""
    global _original_alter, _patched_alter

    table = _load_postgres_table()
    if table is None or is_applied() or _upstream_handles_incompatible_values(table.alter):
        return False

    _original_alter = table.alter

    def compatible_alter(self):
        try:
            return _original_alter(self)
        except Exception as exc:
            if not _is_incompatible_type_error(exc):
                raise

            import frappe
            from frappe import _

            frappe.throw(
                _(
                    "Cannot change field type in {0}: some existing values cannot be converted to the new type"
                ).format(self.doctype),
                title=_("Incompatible Values"),
            )

    _patched_alter = compatible_alter
    table.alter = compatible_alter  # nosemgrep
    return True


def remove():
    """Restore Frappe's original schema implementation if this adapter installed itself."""
    global _original_alter, _patched_alter

    table = _load_postgres_table()
    if table is None or not is_applied():
        return False

    table.alter = _original_alter  # nosemgrep
    _original_alter = None
    _patched_alter = None
    return True
