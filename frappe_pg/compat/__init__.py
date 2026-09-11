"""Runtime compatibility overrides for upstream Frappe and ERPNext behavior."""

from .registry import apply_compatibility_overrides, get_compatibility_status

__all__ = ["apply_compatibility_overrides", "get_compatibility_status"]
