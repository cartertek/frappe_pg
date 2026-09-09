"""Lifecycle hooks for application-level PostgreSQL compatibility overrides."""


def after_app_install(app_name=None):
    """Re-evaluate compatibility adapters after the app set changes.

    ``frappe_pg`` is commonly installed before ERPNext/HRMS. Import-time
    adapter discovery therefore cannot see those apps yet. Frappe calls this
    hook after each app installation, at which point the newly available
    application modules can be inspected and patched idempotently.
    """
    from frappe_pg.compat.registry import apply_compatibility_overrides

    return apply_compatibility_overrides()
