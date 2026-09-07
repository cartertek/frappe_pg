"""Backward-compatible import path for the ERPNext Trends compatibility override."""

from frappe_pg.compat.erpnext.trends_group_by import execute
from frappe_pg.compat.erpnext.trends_group_by import apply as apply_trends_patch

__all__ = ["apply_trends_patch", "execute"]
