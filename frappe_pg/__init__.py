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

# Apply ERPNext period-closing PostgreSQL compatibility backport.
try:
    from frappe_pg.patches.v1.fix_erpnext_period_closing import apply_period_closing_patch
    apply_period_closing_patch()
except Exception:
    # ERPNext might not be installed or available yet.
    pass

# Apply ERPNext future-stock-voucher PostgreSQL locking compatibility backport.
try:
    from frappe_pg.patches.v1.fix_erpnext_future_stock_vouchers import apply_future_stock_vouchers_patch
    apply_future_stock_vouchers_patch()
except Exception:
    # ERPNext might not be installed or available yet.
    pass


# Apply ERPNext batch-valuation PostgreSQL locking compatibility backport.
try:
    from frappe_pg.patches.v1.fix_erpnext_batch_valuation_locking import apply_batch_valuation_locking_patch
    apply_batch_valuation_locking_patch()
except Exception:
    # ERPNext might not be installed or available yet.
    pass



# Apply ERPNext stock-reservation PostgreSQL locking compatibility backport.
try:
    from frappe_pg.patches.v1.fix_erpnext_stock_reservation_locking import (
        apply_stock_reservation_locking_patch,
    )
    apply_stock_reservation_locking_patch()
except Exception:
    # ERPNext might not be installed or available yet.
    pass

# Apply ERPNext payment-terms status PostgreSQL compatibility backport.
try:
    from frappe_pg.patches.v1.fix_erpnext_payment_terms_status import apply_payment_terms_status_patch
    apply_payment_terms_status_patch()
except Exception:
    # ERPNext might not be installed or available yet.
    pass

# Apply ERPNext Pick List PostgreSQL grouped-locking compatibility backport.
try:
    from frappe_pg.patches.v1.fix_erpnext_pick_list_locking import apply_pick_list_locking_patch
    apply_pick_list_locking_patch()
except Exception:
    # ERPNext might not be installed or available yet.
    pass

# Apply ERPNext trends.py patch for GROUP BY compatibility
try:
    from frappe_pg.patches.v1.fix_erpnext_trends import apply_trends_patch
    apply_trends_patch()
except Exception:
    # ERPNext might not be installed or available yet.
    pass

# Keep automatic PostgreSQL index create/drop names symmetric.
try:
    from frappe_pg.patches.v1.fix_postgres_automatic_index_drop import (
        apply_postgres_automatic_index_drop_patch,
    )
    apply_postgres_automatic_index_drop_patch()
except Exception:
    pass

# Apply ERPNext PostgreSQL-safe manufacturing grouping semantics.
try:
    from frappe_pg.patches.v1.fix_erpnext_manufacturing_grouping import apply_manufacturing_grouping_patch

    apply_manufacturing_grouping_patch()
except Exception:
    pass
