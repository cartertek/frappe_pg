"""Preserve Frappe transaction semantics after expected unique insert failures.

MariaDB callers can catch a statement-level unique violation and continue using
the transaction. PostgreSQL marks the transaction failed until rollback. Older
Frappe ``db_insert`` implementations do not isolate that expected failure.
"""

import inspect
import uuid

NAME = "frappe_unique_insert_transaction"

_original_db_insert = None
_patched_db_insert = None


_DATABASE_UNIQUE_DOCTYPES = {
    # ERPNext creates this composite constraint in Bin.on_doctype_update(); it is
    # not represented by any individual DocField.unique flag.
    "Bin",
}


def _load_base_document():
    try:
        from frappe.model.base_document import BaseDocument
    except ImportError:
        return None
    return BaseDocument


def _upstream_isolates_insert_failures(function):
    """Detect native savepoint isolation by capability rather than Frappe version."""
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    return "savepoint" in source and "rollback" in source


def is_applied():
    document = _load_base_document()
    return (
        document is not None and _patched_db_insert is not None and document.db_insert is _patched_db_insert
    )


def is_needed():
    document = _load_base_document()
    return (
        document is not None
        and not is_applied()
        and not _upstream_isolates_insert_failures(document.db_insert)
    )


def _document_has_unique_fields(doc):
    return getattr(doc, "doctype", None) in _DATABASE_UNIQUE_DOCTYPES or any(
        getattr(field, "unique", False) for field in doc.meta.fields
    )


def apply():
    """Add a narrow PostgreSQL savepoint around inserts that can hit unique fields."""
    global _original_db_insert, _patched_db_insert

    document = _load_base_document()
    if document is None or is_applied() or _upstream_isolates_insert_failures(document.db_insert):
        return False

    _original_db_insert = document.db_insert

    def compatible_db_insert(self, *args, **kwargs):
        import frappe

        if getattr(frappe.db, "db_type", None) != "postgres" or not _document_has_unique_fields(self):
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

    _patched_db_insert = compatible_db_insert
    document.db_insert = compatible_db_insert  # nosemgrep
    return True


def remove():
    """Restore Frappe's original insert implementation if this adapter installed itself."""
    global _original_db_insert, _patched_db_insert

    document = _load_base_document()
    if document is None or not is_applied():
        return False

    document.db_insert = _original_db_insert  # nosemgrep
    _original_db_insert = None
    _patched_db_insert = None
    return True
