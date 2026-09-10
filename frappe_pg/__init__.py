"""
Frappe PostgreSQL Compatibility App
===================================

This app provides comprehensive PostgreSQL compatibility for Frappe/ERPNext.

Features:
- Automatic SQL query transformation (MySQL → PostgreSQL)
- Native Frappe SQL execution and transaction semantics
- PostgreSQL compatibility functions (GROUP_CONCAT, unix_timestamp, etc.)
- ERPNext trends report GROUP BY fixes

The patches are applied automatically when this module is imported.

Author: Frappe PostgreSQL Team
License: MIT
Repository: https://github.com/excel-azmin/frappe_pg.git
"""

__version__ = "1.0.0"
__author__ = "Frappe PostgreSQL Team"
__license__ = "MIT"

# Import and apply patches immediately when the app loads
try:
    from frappe_pg.postgres.database_patches import apply_postgres_fixes
    apply_postgres_fixes()
except Exception:
    # During installation, Frappe might not be fully initialized yet.
    pass

# Apply application-level PostgreSQL compatibility overrides.
try:
    from frappe_pg.compat import apply_compatibility_overrides

    apply_compatibility_overrides()
except Exception:
    # Frappe/ERPNext might not be fully available yet during installation.
    pass

# Apply ERPNext PostgreSQL-safe manufacturing grouping semantics.
try:
    from frappe_pg.patches.v1.fix_erpnext_manufacturing_grouping import apply_manufacturing_grouping_patch

    apply_manufacturing_grouping_patch()
except Exception:
    pass
