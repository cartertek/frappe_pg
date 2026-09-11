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


def _document_needs_savepoint(doc):
    """Return whether an insert can hit an expected uniqueness retry path."""
    return (
        getattr(doc, "doctype", None) in _DATABASE_UNIQUE_DOCTYPES
        or getattr(getattr(doc, "meta", None), "autoname", None) == "hash"
        or any(getattr(field, "unique", False) for field in getattr(getattr(doc, "meta", None), "fields", ()))
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

        if getattr(frappe.db, "db_type", None) != "postgres" or not _document_needs_savepoint(self):
            return _original_db_insert(self, *args, **kwargs)

        # Frappe retries ``autoname == "hash"`` primary-key collisions from
        # inside db_insert() by clearing ``name`` and recursively calling
        # ``self.db_insert()``. PostgreSQL has already marked the transaction
        # failed at that point, so the recursive wrapper must restore the
        # active savepoint before Frappe can generate and insert the new hash.
        retry_save_point = getattr(self, "_frappe_pg_insert_savepoint", None)
        if retry_save_point:
            frappe.db.rollback(save_point=retry_save_point)

        save_point = f"frappe_pg_unique_{uuid.uuid4().hex}"
        frappe.db.savepoint(save_point)
        previous_save_point = retry_save_point
        self._frappe_pg_insert_savepoint = save_point
        try:
            result = _original_db_insert(self, *args, **kwargs)
        except Exception as exc:
            frappe.db.rollback(save_point=save_point)

            # A field-based autoname can also be marked unique. PostgreSQL may
            # report that redundant unique index before the primary-key index,
            # causing Frappe to raise UniqueValidationError even though the
            # semantic collision is the document name itself. Preserve Frappe's
            # DuplicateEntryError contract for that narrow redundant-constraint
            # shape while leaving ordinary unique-field violations untouched.
            autoname = getattr(getattr(self, "meta", None), "autoname", "") or ""
            if autoname.startswith("field:") and isinstance(exc, frappe.UniqueValidationError):
                fieldname = autoname.split(":", 1)[1]
                field = getattr(getattr(self, "meta", None), "get_field", lambda _name: None)(fieldname)
                if field and getattr(field, "unique", False) and self.name == self.get(fieldname):
                    raise frappe.DuplicateEntryError(self.doctype, self.name, exc) from exc
            raise
        else:
            frappe.db.release_savepoint(save_point)
            return result
        finally:
            if previous_save_point:
                self._frappe_pg_insert_savepoint = previous_save_point
            else:
                try:
                    delattr(self, "_frappe_pg_insert_savepoint")
                except AttributeError:
                    pass

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
