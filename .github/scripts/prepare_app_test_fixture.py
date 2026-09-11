#!/usr/bin/env python3
"""Run app-global test setup once before snapshotting reusable CI fixtures."""

import argparse
import frappe

try:
    from frappe.tests.utils import make_test_records, toggle_test_mode
except ImportError:  # Frappe v15
    from frappe.test_runner import make_test_records

    def toggle_test_mode(enabled):
        frappe.flags.in_test = enabled


def _run_app_test_setup(app):
    # Match ParallelTestRunner.before_test_setup ordering exactly: app hook first,
    # then the app's declared global test dependencies.
    for fn in frappe.get_hooks("before_tests", app_name=app):
        print(f"Running global test setup hook: {fn}")
        frappe.get_attr(fn)()

    test_module = frappe.get_module(f"{app}.tests")
    for doctype in getattr(test_module, "global_test_dependencies", ()):
        print(f"Creating global test dependency: {doctype}")
        make_test_records(doctype, commit=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--app", required=True)
    args = parser.parse_args()

    frappe.init(site=args.site)
    if not frappe.db:
        frappe.connect()

    toggle_test_mode(True)
    frappe.clear_cache()
    frappe.utils.scheduler.disable_scheduler()

    _run_app_test_setup(args.app)

    frappe.db.commit()  # nosemgrep


if __name__ == "__main__":
    main()
