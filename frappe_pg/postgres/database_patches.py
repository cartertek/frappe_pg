"""
Database Query Transformation Patch for PostgreSQL Compatibility
===============================================================

This module applies PostgreSQL compatibility transformations at Frappe's
``Database._transform_query`` extension point. It deliberately leaves
``PostgresDatabase.sql``, transaction methods, parameter normalization,
tracing, execution, and error handling under Frappe's control.
"""

import json
import re
import uuid

import frappe
from frappe.database.postgres.database import PostgresDatabase  # nosemgrep
from frappe.database.postgres.schema import PostgresSchema
from frappe.model.base_document import BaseDocument

from .db_functions import create_missing_functions
from .query_transformers import apply_all_query_transformations

_original_transform_query = None
_original_transform_result = None
_original_is_deadlocked = None
_original_schema_alter = None
_original_db_insert = None
_patches_applied = False

_JSON_TYPE_OIDS = {114, 3802}


def _normalize_zero_timestamp_params(query, values):
    """Normalize MySQL zero-date pagination values for Frappe timestamp metadata fields."""
    if not isinstance(values, dict):
        return values

    params = set(
        re.findall(
            r'"(?:creation|modified)"\s*(?:>=|>)\s*%\((?P<param>[A-Za-z0-9_]+)\)s',
            query,
            re.IGNORECASE,
        )
    )
    changed = [name for name in params if values.get(name) in {0, "0", "0.0"}]
    if not changed:
        return values

    normalized = values.copy()
    for name in changed:
        normalized[name] = "0001-01-01 00:00:00"
    return normalized


def patched_transform_query(self, query, values):
    """Apply frappe_pg SQL rewrites while preserving Frappe's values contract."""
    query, values = _original_transform_query(self, query, values)
    values = _normalize_zero_timestamp_params(query, values)
    return apply_all_query_transformations(query), values


def _serialize_json_cells(result, description):
    """Return PostgreSQL JSON/JSONB cells using Frappe's string-value contract.

    MariaDB exposes Frappe JSON fields as serialized text. psycopg2 decodes
    PostgreSQL ``json``/``jsonb`` columns to Python objects automatically, which
    breaks Frappe code that subsequently calls ``json.loads``. Only cells whose
    cursor type OID is JSON/JSONB are normalized; arrays and other native
    PostgreSQL result types are left untouched.
    """
    if not result or not description:
        return result

    json_columns = {index for index, column in enumerate(description) if column.type_code in _JSON_TYPE_OIDS}
    if not json_columns:
        return result

    return tuple(
        tuple(
            json.dumps(value) if index in json_columns and isinstance(value, dict | list) else value
            for index, value in enumerate(row)
        )
        for row in result
    )


def patched_transform_result(self, result):
    """Serialize only JSON/JSONB result cells after Frappe's native transform."""
    result = _original_transform_result(self, result)
    return _serialize_json_cells(result, self._cursor.description)


def patched_is_deadlocked(exc):
    """Treat PostgreSQL serialization failures as retriable transaction conflicts."""
    return getattr(exc, "pgcode", None) == "40001" or _original_is_deadlocked(exc)


def _document_has_unique_fields(doc):
    """Return whether an insert can fail on a DocField-level unique constraint."""
    return any(getattr(field, "unique", False) for field in doc.meta.fields)


def patched_db_insert(self, *args, **kwargs):
    """Keep expected insert failures from aborting PostgreSQL's outer transaction.

    MariaDB leaves a transaction usable after a statement-level unique violation;
    PostgreSQL marks it failed until rollback. Frappe callers therefore catch
    ``UniqueValidationError`` and continue without an explicit rollback. For
    DocTypes that actually have unique fields, isolate the insert in a savepoint
    so the PostgreSQL behavior matches that contract without adding savepoint
    overhead to ordinary document inserts.
    """
    if not _document_has_unique_fields(self):
        return _original_db_insert(self, *args, **kwargs)

    save_point = f"frappe_pg_unique_{uuid.uuid4().hex}"
    frappe.db.savepoint(save_point)
    try:
        result = _original_db_insert(self, *args, **kwargs)
    except Exception:
        frappe.db.rollback(save_point=save_point)
        raise
    else:
        frappe.db.release_savepoint(save_point)
        return result


_INCOMPATIBLE_TYPE_PGCODES = {"42804", "22P02", "22003"}


def patched_schema_alter(self):
    """Map PostgreSQL cast failures to Frappe's incompatible-values validation error.

    Frappe develop treats these PostgreSQL errors like MariaDB's truncated-value
    error when changing a DocType field type. Older supported Frappe branches
    leak the driver exception instead.
    """
    try:
        return _original_schema_alter(self)
    except Exception as exc:
        if getattr(exc, "pgcode", None) not in _INCOMPATIBLE_TYPE_PGCODES:
            raise
        raise frappe.ValidationError(
            f"Cannot change field type in {self.doctype}: some existing values cannot be converted to the new type"
        ) from exc


def apply_postgres_fixes():
    """Install the query transformation hook once per process."""
    global \
        _original_transform_query, \
        _original_transform_result, \
        _original_is_deadlocked, \
        _original_schema_alter, \
        _patches_applied

    if _patches_applied:
        return

    _original_transform_query = PostgresDatabase._transform_query
    _original_transform_result = PostgresDatabase._transform_result
    _original_is_deadlocked = PostgresDatabase.is_deadlocked
    _original_schema_alter = PostgresSchema.alter
    _original_db_insert = BaseDocument.db_insert
    PostgresDatabase._transform_query = patched_transform_query  # nosemgrep
    PostgresDatabase._transform_result = patched_transform_result  # nosemgrep
    PostgresDatabase.is_deadlocked = staticmethod(patched_is_deadlocked)  # nosemgrep
    PostgresSchema.alter = patched_schema_alter  # nosemgrep
    BaseDocument.db_insert = patched_db_insert  # nosemgrep
    _patches_applied = True


def remove_postgres_fixes():
    """Restore the transform hook captured when the compatibility patch was applied."""
    global _patches_applied

    if not _patches_applied:
        return

    if PostgresDatabase._transform_query == patched_transform_query:
        PostgresDatabase._transform_query = _original_transform_query  # nosemgrep
    if PostgresDatabase._transform_result == patched_transform_result:
        PostgresDatabase._transform_result = _original_transform_result  # nosemgrep
    if PostgresDatabase.is_deadlocked == patched_is_deadlocked:
        PostgresDatabase.is_deadlocked = staticmethod(_original_is_deadlocked)  # nosemgrep
    if PostgresSchema.alter == patched_schema_alter:
        PostgresSchema.alter = _original_schema_alter  # nosemgrep
    if BaseDocument.db_insert == patched_db_insert:
        BaseDocument.db_insert = _original_db_insert  # nosemgrep

    _patches_applied = False


def on_session_creation(login_manager):
    """Ensure compatibility patches remain applied for new sessions."""
    apply_postgres_fixes()


def after_migrate():
    """Re-apply the transform hook and ensure compatibility functions exist."""
    print("\nApplying post-migration PostgreSQL fixes...")
    apply_postgres_fixes()
    create_missing_functions()


def check_patches_status():
    """Return whether the compatibility transform hook is installed."""
    return {
        "patches_applied": _patches_applied,
        "transform_query_patched": (
            PostgresDatabase._transform_query == patched_transform_query if _patches_applied else False
        ),
        "sql_patched": False,
        "commit_patched": False,
        "rollback_patched": False,
    }


try:
    apply_postgres_fixes()
except Exception:
    # Import-time initialization can occur before Frappe is ready.
    pass
