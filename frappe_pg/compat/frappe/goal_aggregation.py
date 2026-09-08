"""Backport Frappe's PostgreSQL goal aggregation fix when upstream still needs it.

Upstream fix: frappe/frappe commit 181f1e079d
"fix(postgres): aggregate the goal column, not a string literal".
"""

import ast
import inspect
import textwrap
from importlib import import_module

NAME = "frappe_goal_aggregation"

_original_get_monthly_results = None
_patched_get_monthly_results = None


def _load_goal_module():
    try:
        return import_module("frappe.utils.goal")
    except ImportError:
        return None


def _uses_string_literal_goal_field(function):
    """Detect the pre-fix Query Builder call that passes the field name as a string."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, SyntaxError):
        return False

    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "Function"
        ):
            continue
        if len(node.args) < 2:
            continue
        aggregation, value = node.args[:2]
        if (
            isinstance(aggregation, ast.Name)
            and aggregation.id == "aggregation"
            and isinstance(value, ast.Name)
            and value.id == "goal_field"
        ):
            return True
    return False


def is_needed():
    goal = _load_goal_module()
    return goal is not None and not is_applied() and _uses_string_literal_goal_field(goal.get_monthly_results)


def is_applied():
    goal = _load_goal_module()
    return (
        goal is not None
        and _patched_get_monthly_results is not None
        and (goal.get_monthly_results is _patched_get_monthly_results)
    )


def apply():
    """Install Frappe's upstream PostgreSQL fix only while the installed function is still affected."""
    global _original_get_monthly_results, _patched_get_monthly_results

    goal = _load_goal_module()
    if goal is None or is_applied() or not _uses_string_literal_goal_field(goal.get_monthly_results):
        return False

    _original_get_monthly_results = goal.get_monthly_results

    def compatible_get_monthly_results(
        goal_doctype,
        goal_field,
        date_col,
        filters,
        aggregation="sum",
    ):
        import frappe
        from frappe.query_builder.functions import DateFormat, Function
        from frappe.query_builder.utils import DocType

        if aggregation.lower() not in {"sum", "avg", "count", "min", "max"}:
            frappe.throw(f"Invalid aggregation type: {aggregation}")

        valid_fields = frappe.get_meta(goal_doctype).get_valid_columns()
        if goal_field not in valid_fields:
            frappe.throw(f"Invalid goal field: {goal_field}")
        if date_col not in valid_fields:
            frappe.throw(f"Invalid date field: {date_col}")

        Table = DocType(goal_doctype)
        date_format = "%m-%Y" if frappe.db.db_type != "postgres" else "MM-YYYY"

        return dict(
            frappe.qb.get_query(
                table=goal_doctype,
                fields=[
                    DateFormat(Table[date_col], date_format).as_("month_year"),
                    Function(aggregation, Table[goal_field]),
                ],
                filters=filters,
                ignore_permissions=False,
            )
            .groupby("month_year")
            .run()
        )

    _patched_get_monthly_results = compatible_get_monthly_results
    goal.get_monthly_results = compatible_get_monthly_results  # nosemgrep
    return True


def remove():
    """Restore the upstream function if this module installed the override."""
    global _original_get_monthly_results, _patched_get_monthly_results

    goal = _load_goal_module()
    if goal is None or not is_applied():
        return False

    goal.get_monthly_results = _original_get_monthly_results  # nosemgrep
    _original_get_monthly_results = None
    _patched_get_monthly_results = None
    return True
