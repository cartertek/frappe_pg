"""Backport Frappe's database-aware Month/MonthName/Quarter query functions.

Upstream fix: frappe/frappe commit 49711c52db
``fix(postgres): make Month/MonthName/Quarter db-aware``.
"""

import inspect
from importlib import import_module

NAME = "frappe_postgres_date_functions"
UPSTREAM_COMMIT = "49711c52db"

_original_initializers = {}
_patched_initializers = {}


def _load_custom_module():
    try:
        return import_module("frappe.query_builder.custom")
    except ImportError:
        return None


def _handles_postgres(initializer):
    try:
        source = inspect.getsource(initializer)
    except (OSError, TypeError):
        return False
    return "_is_postgres" in source and ("to_char" in source or "date_part" in source)


def _targets(module):
    if module is None:
        return {}
    return {
        name: getattr(module, name)
        for name in ("MonthName", "Month", "Quarter")
        if getattr(module, name, None) is not None
    }


def is_applied():
    module = _load_custom_module()
    if module is None:
        return False
    return any(
        name in _patched_initializers and cls.__init__ is _patched_initializers[name]
        for name, cls in _targets(module).items()
    )


def is_needed():
    module = _load_custom_module()
    targets = _targets(module)
    return (
        bool(targets)
        and not is_applied()
        and any(not _handles_postgres(cls.__init__) for cls in targets.values())
    )


def _is_postgres():
    import frappe

    return bool(getattr(frappe, "db", None)) and frappe.db.db_type == "postgres"


def apply():
    """Make the affected custom query functions render PostgreSQL equivalents."""
    module = _load_custom_module()
    targets = _targets(module)
    if not targets or is_applied():
        return False

    changed = False
    for name, cls in targets.items():
        if _handles_postgres(cls.__init__):
            continue

        original = cls.__init__
        _original_initializers[name] = original

        if name == "MonthName":

            def compatible_init(self, field, alias=None, _original=original):
                if _is_postgres():
                    module.Function.__init__(self, "to_char", field, "FMMonth", alias=alias)
                else:
                    _original(self, field, alias=alias)

        else:
            part = name.lower()

            def compatible_init(self, field, alias=None, _original=original, _part=part):
                if _is_postgres():
                    module.Function.__init__(self, "date_part", _part, field, alias=alias)
                else:
                    _original(self, field, alias=alias)

        _patched_initializers[name] = compatible_init
        cls.__init__ = compatible_init  # nosemgrep
        changed = True

    return changed


def remove():
    """Restore every initializer replaced by this adapter."""
    module = _load_custom_module()
    if module is None:
        return False

    changed = False
    for name, cls in _targets(module).items():
        patched = _patched_initializers.get(name)
        original = _original_initializers.get(name)
        if patched is not None and original is not None and cls.__init__ is patched:
            cls.__init__ = original  # nosemgrep
            changed = True

    _original_initializers.clear()
    _patched_initializers.clear()
    return changed
