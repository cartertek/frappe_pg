"""Keep Frappe automatic index create/drop names symmetric on PostgreSQL."""

import inspect
import re

NAME = "frappe_postgres_automatic_index_drop"
_original_alter = None
_patched_alter = None


def _load_postgres_table():
    try:
        from frappe.database.postgres.schema import PostgresTable
    except ImportError:
        return None
    return PostgresTable


def _needs_patch(function):
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    return 'DROP INDEX IF EXISTS "{col.fieldname}"' in source


def is_applied():
    table = _load_postgres_table()
    return table is not None and _patched_alter is not None and table.alter is _patched_alter


def is_needed():
    table = _load_postgres_table()
    return table is not None and not is_applied() and _needs_patch(table.alter)


def _rewrite_automatic_drop(query, table_name):
    if not isinstance(query, str) or "DROP INDEX" not in query.upper():
        return query

    pattern = re.compile(r'DROP\s+INDEX\s+IF\s+EXISTS\s+"(?P<index>[^"]+)"', re.IGNORECASE)

    def replace(match):
        index = match.group("index")
        # Explicit names (for example unique_* or an already-qualified automatic
        # name) are not Frappe's bare field-name automatic indexes.
        if index.startswith("unique_") or index.startswith(f"{table_name}_"):
            return match.group(0)
        return f'DROP INDEX IF EXISTS "{table_name}_{index}_index"'

    return pattern.sub(replace, query)


def apply():
    global _original_alter, _patched_alter

    table = _load_postgres_table()
    if table is None or is_applied() or not _needs_patch(table.alter):
        return False

    _original_alter = table.alter

    def compatible_alter(self, *args, **kwargs):
        import frappe

        had_instance_sql = "sql" in getattr(frappe.db, "__dict__", {})
        original_instance_sql = getattr(frappe.db, "__dict__", {}).get("sql")
        original_sql = frappe.db.sql

        def compatible_sql(query, *sql_args, **sql_kwargs):
            return original_sql(_rewrite_automatic_drop(query, self.table_name), *sql_args, **sql_kwargs)

        frappe.db.sql = compatible_sql  # nosemgrep
        try:
            return _original_alter(self, *args, **kwargs)
        finally:
            if had_instance_sql:
                frappe.db.sql = original_instance_sql  # nosemgrep
            else:
                delattr(frappe.db, "sql")

    _patched_alter = compatible_alter
    table.alter = compatible_alter  # nosemgrep
    return True


def remove():
    global _original_alter, _patched_alter

    table = _load_postgres_table()
    if table is None or not is_applied():
        return False
    table.alter = _original_alter  # nosemgrep
    _original_alter = None
    _patched_alter = None
    return True
