"""ERPNext Trends GROUP BY compatibility override for PostgreSQL."""

from importlib import import_module

NAME = "erpnext_trends_group_by"

_original_based_wise_columns_query = None
_patched_based_wise_columns_query = None


def _load_trends_module():
    try:
        return import_module("erpnext.controllers.trends")
    except ImportError:
        return None


def _normalize_group_by(based_on_details, based_on, trans):
    current_group_by = based_on_details.get("based_on_group_by", "")
    selected = based_on_details.get("based_on_select", "")

    if based_on == "Item" and current_group_by == "t2.item_code":
        current_group_by = "t2.item_code, t2.item_name"
    elif based_on == "Customer":
        if trans == "Quotation" and "party_name" in selected:
            for column in ("t1.customer_name", "t1.territory"):
                if column not in current_group_by:
                    current_group_by += f", {column}"
        elif "customer_name" in selected:
            for column in ("t1.customer_name", "t1.territory"):
                if column not in current_group_by:
                    current_group_by += f", {column}"
    elif based_on == "Supplier" and "supplier_name" in selected:
        if "t1.supplier_name" not in current_group_by:
            current_group_by += ", t1.supplier_name"
    elif based_on == "Project" and "project_name" in selected:
        if "t2.project_name" not in current_group_by:
            current_group_by += ", t2.project_name"

    if "default_currency" in selected and "t4.default_currency" not in current_group_by:
        current_group_by += ", t4.default_currency"

    based_on_details["based_on_group_by"] = current_group_by
    return based_on_details


def _upstream_needs_override(trends):
    """Probe stable, side-effect-free query metadata returned by ERPNext."""
    probes = (
        ("Item", "Sales Order"),
        ("Customer", "Sales Order"),
        ("Supplier", "Purchase Order"),
    )
    for based_on, trans in probes:
        original = trends.based_wise_columns_query(based_on, trans)
        normalized = _normalize_group_by(dict(original), based_on, trans)
        if normalized.get("based_on_group_by") != original.get("based_on_group_by"):
            return True
    return False


def is_needed():
    trends = _load_trends_module()
    if trends is None or is_applied():
        return False
    return _upstream_needs_override(trends)


def is_applied():
    trends = _load_trends_module()
    return trends is not None and _patched_based_wise_columns_query is not None and (
        trends.based_wise_columns_query is _patched_based_wise_columns_query
    )


def apply():
    """Install the override once when the installed ERPNext still needs it."""
    global _original_based_wise_columns_query, _patched_based_wise_columns_query

    trends = _load_trends_module()
    if trends is None or is_applied() or not _upstream_needs_override(trends):
        return False

    _original_based_wise_columns_query = trends.based_wise_columns_query

    def compatible_based_wise_columns_query(based_on, trans):
        result = _original_based_wise_columns_query(based_on, trans)
        return _normalize_group_by(result, based_on, trans)

    _patched_based_wise_columns_query = compatible_based_wise_columns_query
    trends.based_wise_columns_query = compatible_based_wise_columns_query  # nosemgrep
    return True


def remove():
    """Restore the upstream function if this module installed the override."""
    global _original_based_wise_columns_query, _patched_based_wise_columns_query

    trends = _load_trends_module()
    if trends is None or not is_applied():
        return False

    trends.based_wise_columns_query = _original_based_wise_columns_query  # nosemgrep
    _original_based_wise_columns_query = None
    _patched_based_wise_columns_query = None
    return True


def execute():
    """Compatibility entry point retained for Frappe patch runners."""
    return apply()
