#!/usr/bin/env python3
"""Run app-global test setup once before snapshotting reusable CI fixtures."""

import argparse
import importlib.util

import frappe

try:
    from frappe.tests.utils import make_test_records, toggle_test_mode
except ImportError:  # Frappe v15
    from frappe.test_runner import make_test_records

    def toggle_test_mode(enabled):
        frappe.flags.in_test = enabled


def _erpnext_v16_bootstrap():
    """Run ERPNext v16+ BootStrapTestData when the trigger module exists."""
    bootstrap_module = "erpnext.tests.bootstrap_test_data"
    if importlib.util.find_spec(bootstrap_module) is None:
        return False
    print("Running ERPNext bootstrap test data setup")
    frappe.get_module(bootstrap_module)
    return True


def _run_app_test_setup(app):
    # Match ParallelTestRunner.before_test_setup ordering exactly: app hook first,
    # then the app's declared global test dependencies.
    for fn in frappe.get_hooks("before_tests", app_name=app):
        print(f"Running global test setup hook: {fn}")
        frappe.get_attr(fn)()

    if app == "erpnext":
        _erpnext_v16_bootstrap()

    test_module = frappe.get_module(f"{app}.tests")
    for doctype in getattr(test_module, "global_test_dependencies", ()):
        print(f"Creating global test dependency: {doctype}")
        make_test_records(doctype, commit=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument(
        "--phase",
        choices=("all", "hrms-base"),
        default="all",
        help="hrms-base runs only the v16 ERPNext bootstrap needed before HRMS; v15 is intentionally a no-op.",
    )
    args = parser.parse_args()

    frappe.init(site=args.site)
    if not frappe.db:
        frappe.connect()

    toggle_test_mode(True)
    frappe.clear_cache()
    frappe.utils.scheduler.disable_scheduler()

    if args.phase == "hrms-base":
        if args.app != "erpnext":
            raise SystemExit("hrms-base phase is only valid for ERPNext")
        # v15 must remain company-free so HRMS before_tests executes setup_complete
        # for _Test Company. v16 needs BootStrapTestData first because importing
        # erpnext.tests.utils later otherwise collides with installed master rows.
        _erpnext_v16_bootstrap()
    else:
        _run_app_test_setup(args.app)

    frappe.db.commit()  # nosemgrep


if __name__ == "__main__":
    main()
