"""Backport Frappe's PostgreSQL unique-index violation classification."""

import inspect

NAME = "frappe_postgres_unique_violation"

_original_is_unique_key_violation = None
_patched_is_unique_key_violation = None


def _load_postgres_database():
    try:
        from frappe.database.postgres.database import PostgresDatabase
    except ImportError:
        return None
    return PostgresDatabase


def _upstream_handles_custom_unique_indexes(function):
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    return "is_primary_key_violation" in source and "is_duplicate_entry" in source


def is_applied():
    database = _load_postgres_database()
    return (
        database is not None
        and _patched_is_unique_key_violation is not None
        and database.is_unique_key_violation is _patched_is_unique_key_violation
    )


def is_needed():
    database = _load_postgres_database()
    return (
        database is not None
        and not is_applied()
        and not _upstream_handles_custom_unique_indexes(database.is_unique_key_violation)
    )


def apply():
    """Classify every non-primary-key PostgreSQL unique violation as a Frappe unique-key violation."""
    global _original_is_unique_key_violation, _patched_is_unique_key_violation
    database = _load_postgres_database()
    if (
        database is None
        or is_applied()
        or _upstream_handles_custom_unique_indexes(database.is_unique_key_violation)
    ):
        return False

    _original_is_unique_key_violation = database.is_unique_key_violation

    def compatible_is_unique_key_violation(exc):
        return database.is_duplicate_entry(exc) and not database.is_primary_key_violation(exc)

    _patched_is_unique_key_violation = staticmethod(compatible_is_unique_key_violation)
    database.is_unique_key_violation = _patched_is_unique_key_violation  # nosemgrep
    return True


def remove():
    global _original_is_unique_key_violation, _patched_is_unique_key_violation
    database = _load_postgres_database()
    if database is None or not is_applied():
        return False
    database.is_unique_key_violation = _original_is_unique_key_violation  # nosemgrep
    _original_is_unique_key_violation = None
    _patched_is_unique_key_violation = None
    return True
