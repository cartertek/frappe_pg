"""
Database Method Patches for PostgreSQL Compatibility
====================================================

This module applies PostgreSQL compatibility transformations at Frappe's
``Database._transform_query`` extension point.  It deliberately leaves
``PostgresDatabase.sql`` untouched so Frappe retains ownership of query-value
normalization, tracing, transaction checks, execution, and error handling.
"""

import frappe
from frappe.database.postgres.database import PostgresDatabase

from .db_functions import create_missing_functions
from .query_transformers import apply_all_query_transformations

_original_transform_query = None
_original_commit = None
_original_rollback = None
_patches_applied = False


def patched_transform_query(self, query, values):
	"""Apply frappe_pg SQL rewrites while preserving Frappe's values contract."""
	query, values = _original_transform_query(self, query, values)
	return apply_all_query_transformations(query), values


def patched_commit(self):
	"""Preserve the existing commit logging behavior."""
	try:
		return _original_commit(self)
	except Exception as exc:
		frappe.log_error(title="PostgreSQL Commit Failed", message=str(exc))
		raise


def patched_rollback(self):
	"""Preserve the existing rollback behavior without cascading log failures."""
	try:
		return _original_rollback(self)
	except Exception:
		return None


def apply_postgres_fixes():
	"""Apply the PostgreSQL compatibility patches once per process."""
	global _original_transform_query, _original_commit, _original_rollback, _patches_applied

	if _patches_applied:
		return

	print("=" * 60)
	print("Applying PostgreSQL Compatibility Patches for ERPNext")
	print("=" * 60)

	_original_transform_query = PostgresDatabase._transform_query
	_original_commit = PostgresDatabase.commit
	_original_rollback = PostgresDatabase.rollback

	PostgresDatabase._transform_query = patched_transform_query
	PostgresDatabase.commit = patched_commit
	PostgresDatabase.rollback = patched_rollback

	_patches_applied = True

	print("✓ Query transformation hook applied")
	print("✓ Frappe PostgresDatabase.sql left unchanged")
	print("✓ Commit/rollback error handling configured")
	print()
	print("The following transformations are now active:")
	print("  • FORCE/USE/IGNORE INDEX removal")
	print("  • IF() → CASE WHEN conversion")
	print("  • IFNULL() → COALESCE() conversion")
	print("  • DATE_FORMAT() → TO_CHAR() conversion")
	print("=" * 60)


def remove_postgres_fixes():
	"""Restore the Frappe methods captured when the compatibility patch was applied."""
	global _patches_applied

	if not _patches_applied:
		return

	if PostgresDatabase._transform_query == patched_transform_query:
		PostgresDatabase._transform_query = _original_transform_query
	if PostgresDatabase.commit == patched_commit:
		PostgresDatabase.commit = _original_commit
	if PostgresDatabase.rollback == patched_rollback:
		PostgresDatabase.rollback = _original_rollback

	_patches_applied = False


def on_session_creation(login_manager):
	"""Ensure compatibility patches remain applied for new sessions."""
	apply_postgres_fixes()


def after_migrate():
	"""Re-apply patches and ensure compatibility functions exist after migrations."""
	print("\nApplying post-migration PostgreSQL fixes...")
	apply_postgres_fixes()
	create_missing_functions()


def check_patches_status():
	"""Return whether the compatibility hooks are currently installed."""
	return {
		"patches_applied": _patches_applied,
		"transform_query_patched": (
			PostgresDatabase._transform_query == patched_transform_query if _patches_applied else False
		),
		"sql_patched": False,
		"commit_patched": PostgresDatabase.commit == patched_commit if _patches_applied else False,
		"rollback_patched": PostgresDatabase.rollback == patched_rollback if _patches_applied else False,
	}


try:
	apply_postgres_fixes()
except Exception as exc:
	print(f"Warning: Could not apply PostgreSQL patches during module import: {exc}")
