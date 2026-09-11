"""Backward-compatible import path for the ERPNext Trends compatibility override."""

from frappe_pg.compat.erpnext.trends_group_by import apply as apply_trends_patch
from frappe_pg.compat.erpnext.trends_group_by import execute

__all__ = ["apply_trends_patch", "execute"]
