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
except Exception as e:
    # During installation, frappe might not be fully initialized
    print(f"frappe_pg: Will apply database patches later: {e}")

# Apply application-level PostgreSQL compatibility overrides.
try:
    from frappe_pg.compat import apply_compatibility_overrides

    apply_compatibility_overrides()
except Exception as e:
    # Frappe/ERPNext might not be fully available yet during installation.
    print(f"frappe_pg: Will apply compatibility overrides later: {e}")
