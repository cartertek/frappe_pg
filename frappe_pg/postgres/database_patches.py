"""
Database Query Transformation Patch for PostgreSQL Compatibility
===============================================================

This module applies PostgreSQL compatibility transformations at Frappe's
``Database._transform_query`` extension point. It deliberately leaves
``PostgresDatabase.sql``, transaction methods, parameter normalization,
tracing, execution, and error handling under Frappe's control.
"""

import hashlib
import json
import re
import time
from datetime import time as datetime_time
from datetime import timedelta

import frappe
from frappe.database.postgres.database import PostgresDatabase
from frappe.exceptions import QueryTimeoutError

from .db_functions import create_missing_functions
from .query_transformers import apply_all_query_transformations

_original_transform_query = None
_original_transform_result = None
_original_is_deadlocked = None
_original_get_table_columns_description = None
_had_transaction_advisory_lock = False
_original_transaction_advisory_lock = None
_patches_applied = False

_JSON_TYPE_OIDS = {114, 3802}
_TIME_TYPE_OID = 1083


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


def _normalize_item_attribute_values(query, values):
    """Match varchar Item Attribute values against string parameters on PostgreSQL."""
    if not isinstance(values, dict):
        return values
    params = set(
        re.findall(
            r'(?:"tabItem Attribute Value"\.)?"?attribute_value"?\s*=\s*%\((?P<param>[A-Za-z_][A-Za-z0-9_]*)\)s',
            query,
            re.IGNORECASE,
        )
    )
    changed = [
        name for name in params if values.get(name) is not None and not isinstance(values.get(name), str)
    ]
    if not changed:
        return values
    normalized = values.copy()
    for name in changed:
        normalized[name] = str(normalized[name])
    return normalized


def patched_transform_query(self, query, values):
    """Apply frappe_pg SQL rewrites while preserving Frappe's values contract."""
    query, values = _original_transform_query(self, query, values)
    values = _normalize_zero_timestamp_params(query, values)
    values = _normalize_item_attribute_values(query, values)
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


def _normalize_time_cells(result, description):
    """Expose PostgreSQL TIME cells using MariaDB/PyMySQL's timedelta contract.

    Frappe application code treats Time fields as durations and performs
    arithmetic such as ``datetime + value`` and ``value + timedelta``. PyMySQL
    returns SQL TIME as ``timedelta`` while psycopg2 returns ``datetime.time``.
    Normalize only columns whose cursor OID is PostgreSQL TIME (1083).
    """
    if not result or not description:
        return result

    time_columns = {index for index, column in enumerate(description) if column.type_code == _TIME_TYPE_OID}
    if not time_columns:
        return result

    def as_timedelta(value):
        if not isinstance(value, datetime_time):
            return value
        return timedelta(
            hours=value.hour,
            minutes=value.minute,
            seconds=value.second,
            microseconds=value.microsecond,
        )

    return tuple(
        tuple(as_timedelta(value) if index in time_columns else value for index, value in enumerate(row))
        for row in result
    )


def patched_transform_result(self, result):
    """Normalize PostgreSQL result types to Frappe's MariaDB-facing contracts."""
    result = _original_transform_result(self, result)
    result = _serialize_json_cells(result, self._cursor.description)
    return _normalize_time_cells(result, self._cursor.description)


def patched_is_deadlocked(exc):
    """Treat PostgreSQL serialization failures as retriable transaction conflicts."""
    return getattr(exc, "pgcode", None) == "40001" or _original_is_deadlocked(exc)


def patched_get_table_columns_description(self, table_name):
    """Normalize PostgreSQL TIME metadata to Frappe's declared ``time(6)`` type.

    ``information_schema.columns.data_type`` reports ``time without time zone``
    while Frappe's PostgreSQL type map declares Time as ``time(6)``. Without
    normalization every schema sync schedules the same no-op type alteration.
    """
    columns = _original_get_table_columns_description(self, table_name)
    for column in columns:
        if column.get("type") == "time without time zone":
            column["type"] = "time(6)"
    return columns


def _advisory_lock_key(key) -> int:
    return int.from_bytes(hashlib.sha256(str(key).encode()).digest()[:8], "big", signed=True)


def transaction_advisory_lock(self, key, *, timeout=10):
    """Backport Frappe's PostgreSQL transaction-scoped advisory lock."""
    lock_key = _advisory_lock_key(key)
    deadline = time.monotonic() + timeout
    while not self.sql("SELECT pg_try_advisory_xact_lock(%s)", (lock_key,))[0][0]:
        if time.monotonic() >= deadline:
            raise QueryTimeoutError(f"Could not acquire advisory lock {key!r} within {timeout}s")
        time.sleep(0.1)


def _replace_class_attribute(target, name, value):
    """Assign a compatibility hook without hard-coding a framework attribute assignment."""
    setattr(target, name, value)


def apply_postgres_fixes():
    """Install the query transformation hook once per process."""
    global _original_transform_query, _original_transform_result, _original_is_deadlocked
    global _original_get_table_columns_description
    global _had_transaction_advisory_lock, _original_transaction_advisory_lock, _patches_applied

    if _patches_applied:
        return

    _original_transform_query = PostgresDatabase._transform_query
    _original_transform_result = PostgresDatabase._transform_result
    _original_is_deadlocked = PostgresDatabase.is_deadlocked
    _original_get_table_columns_description = PostgresDatabase.get_table_columns_description
    _had_transaction_advisory_lock = hasattr(PostgresDatabase, "transaction_advisory_lock")
    _original_transaction_advisory_lock = getattr(PostgresDatabase, "transaction_advisory_lock", None)
    _replace_class_attribute(PostgresDatabase, "_transform_query", patched_transform_query)
    _replace_class_attribute(PostgresDatabase, "_transform_result", patched_transform_result)
    _replace_class_attribute(PostgresDatabase, "is_deadlocked", staticmethod(patched_is_deadlocked))
    _replace_class_attribute(
        PostgresDatabase, "get_table_columns_description", patched_get_table_columns_description
    )
    if not _had_transaction_advisory_lock:
        _replace_class_attribute(PostgresDatabase, "transaction_advisory_lock", transaction_advisory_lock)
    _patches_applied = True


def remove_postgres_fixes():
    """Restore the transform hook captured when the compatibility patch was applied."""
    global _patches_applied

    if not _patches_applied:
        return

    if PostgresDatabase._transform_query == patched_transform_query:
        _replace_class_attribute(PostgresDatabase, "_transform_query", _original_transform_query)
    if PostgresDatabase._transform_result == patched_transform_result:
        _replace_class_attribute(PostgresDatabase, "_transform_result", _original_transform_result)
    if PostgresDatabase.is_deadlocked == patched_is_deadlocked:
        _replace_class_attribute(PostgresDatabase, "is_deadlocked", staticmethod(_original_is_deadlocked))
    if PostgresDatabase.get_table_columns_description == patched_get_table_columns_description:
        _replace_class_attribute(
            PostgresDatabase, "get_table_columns_description", _original_get_table_columns_description
        )
    if (
        not _had_transaction_advisory_lock
        and getattr(PostgresDatabase, "transaction_advisory_lock", None) == transaction_advisory_lock
    ):
        delattr(PostgresDatabase, "transaction_advisory_lock")

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
