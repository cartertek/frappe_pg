"""Backport Frappe's PostgreSQL boolean-value normalization.

Frappe stores Check fields as ``smallint`` on PostgreSQL. Older Frappe releases
can emit Python booleans as SQL ``true``/``false`` from Query Builder and can
turn bound booleans into ``"True"``/``"False"`` because ``bool`` subclasses
``int``. Frappe develop fixed both paths in commit 495100d51d.
"""

import inspect
from importlib import import_module

NAME = "frappe_postgres_boolean_values"
UPSTREAM_COMMIT = "495100d51d"

_original_value_get_sql = None
_patched_value_get_sql = None
_original_modify_values = None
_patched_modify_values = None


def _load_targets():
    try:
        terms = import_module("frappe.query_builder.terms")
        postgres = import_module("frappe.database.postgres.database")
    except ImportError:
        return None, None
    return terms, postgres


def _value_wrapper_handles_bool(method):
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return "isinstance(self.value, bool)" in source


def _modify_values_handles_bool(function):
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    return "isinstance(value, bool)" in source


def is_applied():
    terms, postgres = _load_targets()
    if terms is None or postgres is None:
        return False
    value_applied = (
        _patched_value_get_sql is not None
        and terms.ParameterizedValueWrapper.get_sql is _patched_value_get_sql
    )
    values_applied = _patched_modify_values is not None and postgres.modify_values is _patched_modify_values
    return value_applied or values_applied


def is_needed():
    terms, postgres = _load_targets()
    if terms is None or postgres is None or is_applied():
        return False
    return not (
        _value_wrapper_handles_bool(terms.ParameterizedValueWrapper.get_sql)
        and _modify_values_handles_bool(postgres.modify_values)
    )


def apply():
    """Normalize Query Builder literals and bound PostgreSQL booleans to 1/0."""
    global _original_value_get_sql, _patched_value_get_sql
    global _original_modify_values, _patched_modify_values

    terms, postgres = _load_targets()
    if terms is None or postgres is None or is_applied():
        return False

    changed = False
    if not _value_wrapper_handles_bool(terms.ParameterizedValueWrapper.get_sql):
        _original_value_get_sql = terms.ParameterizedValueWrapper.get_sql
        original_value_get_sql = _original_value_get_sql

        def compatible_value_get_sql(self, *args, **kwargs):
            if isinstance(self.value, bool):
                self.value = str(int(self.value))
            return original_value_get_sql(self, *args, **kwargs)

        _patched_value_get_sql = compatible_value_get_sql
        terms.ParameterizedValueWrapper.get_sql = compatible_value_get_sql  # nosemgrep
        changed = True

    if not _modify_values_handles_bool(postgres.modify_values):
        _original_modify_values = postgres.modify_values
        original_modify_values = _original_modify_values

        def compatible_modify_values(values):
            def normalize(value):
                if isinstance(value, bool):
                    return str(int(value))
                if isinstance(value, list | tuple):
                    converted = [normalize(item) for item in value]
                    return tuple(converted) if isinstance(value, tuple) else converted
                return value

            if isinstance(values, dict):
                values = {key: normalize(value) for key, value in values.items()}
            elif isinstance(values, tuple):
                values = tuple(normalize(value) for value in values)
            elif isinstance(values, list):
                values = [normalize(value) for value in values]
            elif isinstance(values, bool):
                values = str(int(values))
            return original_modify_values(values)

        _patched_modify_values = compatible_modify_values
        postgres.modify_values = compatible_modify_values  # nosemgrep
        changed = True

    return changed


def remove():
    """Restore whichever upstream callables this adapter replaced."""
    global _original_value_get_sql, _patched_value_get_sql
    global _original_modify_values, _patched_modify_values

    terms, postgres = _load_targets()
    if terms is None or postgres is None:
        return False

    changed = False
    if (
        _patched_value_get_sql is not None
        and terms.ParameterizedValueWrapper.get_sql is _patched_value_get_sql
    ):
        terms.ParameterizedValueWrapper.get_sql = _original_value_get_sql  # nosemgrep
        changed = True
    if _patched_modify_values is not None and postgres.modify_values is _patched_modify_values:
        postgres.modify_values = _original_modify_values  # nosemgrep
        changed = True

    _original_value_get_sql = None
    _patched_value_get_sql = None
    _original_modify_values = None
    _patched_modify_values = None
    return changed
