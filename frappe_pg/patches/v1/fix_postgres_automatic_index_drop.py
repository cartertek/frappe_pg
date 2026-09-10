"""Keep Frappe automatic index create/drop names symmetric on PostgreSQL."""

import inspect

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


def apply_postgres_automatic_index_drop_patch():
    """Drop the table-qualified automatic index names created by frappe_pg."""
    global _original_alter, _patched_alter

    table = _load_postgres_table()
    if table is None or (_patched_alter is not None and table.alter is _patched_alter):
        return False
    if not _needs_patch(table.alter):
        return False

    _original_alter = table.alter

    def compatible_alter(self, *args, **kwargs):
        renamed = []
        for column in getattr(self, "drop_index", ()):
            fieldname = getattr(column, "fieldname", None)
            if not fieldname:
                continue
            renamed.append((column, fieldname))
            column.fieldname = f"{self.table_name}_{fieldname}_index"
        try:
            return _original_alter(self, *args, **kwargs)
        finally:
            for column, fieldname in renamed:
                column.fieldname = fieldname

    _patched_alter = compatible_alter
    table.alter = compatible_alter  # nosemgrep
    return True
