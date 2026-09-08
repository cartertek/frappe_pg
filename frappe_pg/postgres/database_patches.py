"""
Database Query Transformation Patch for PostgreSQL Compatibility
===============================================================

This module applies PostgreSQL compatibility transformations at Frappe's
``Database._transform_query`` extension point. It deliberately leaves
``PostgresDatabase.sql``, transaction methods, parameter normalization,
tracing, execution, and error handling under Frappe's control.
"""

from frappe.database.postgres.database import PostgresDatabase  # nosemgrep

from .db_functions import create_missing_functions
from .query_transformers import apply_all_query_transformations

_original_transform_query = None
_original_is_deadlocked = None
_patches_applied = False


def patched_transform_query(self, query, values):
    """Apply frappe_pg SQL rewrites while preserving Frappe's values contract."""
    query, values = _original_transform_query(self, query, values)
    return apply_all_query_transformations(query), values


def patched_is_deadlocked(exc):
    """Treat PostgreSQL serialization failures as retriable transaction conflicts."""
    return getattr(exc, "pgcode", None) == "40001" or _original_is_deadlocked(exc)


def apply_postgres_fixes():
    """Install the query transformation hook once per process."""
    global _original_transform_query, _original_is_deadlocked, _patches_applied

    if _patches_applied:
        return

    _original_transform_query = PostgresDatabase._transform_query
    _original_is_deadlocked = PostgresDatabase.is_deadlocked
    PostgresDatabase._transform_query = patched_transform_query  # nosemgrep
    PostgresDatabase.is_deadlocked = staticmethod(patched_is_deadlocked)  # nosemgrep
    _patches_applied = True


def remove_postgres_fixes():
    """Restore the transform hook captured when the compatibility patch was applied."""
    global _patches_applied

    if not _patches_applied:
        return

    if PostgresDatabase._transform_query == patched_transform_query:
        PostgresDatabase._transform_query = _original_transform_query  # nosemgrep
    if PostgresDatabase.is_deadlocked == patched_is_deadlocked:
        PostgresDatabase.is_deadlocked = staticmethod(_original_is_deadlocked)  # nosemgrep

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
