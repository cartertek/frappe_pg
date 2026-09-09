#!/usr/bin/env python3
"""Run an app's global test setup once before snapshotting a reusable CI fixture."""

import argparse

import frappe

try:
    from frappe.tests.utils import make_test_records, toggle_test_mode
except ImportError:  # Frappe v15
    from frappe.test_runner import make_test_records

    def toggle_test_mode(enabled):
        frappe.flags.in_test = enabled


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

    for fn in frappe.get_hooks("before_tests", app_name=args.app):
        print(f"Running global test setup hook: {fn}")
        frappe.get_attr(fn)()

    if args.app == "erpnext":
        # ERPNext v16+ ships a CI-only trigger module whose import instantiates
        # BootStrapTestData.  Older branches (including v15) do not have it and
        # must not be forced through a module that does not exist.
        import importlib.util

        bootstrap_module = "erpnext.tests.bootstrap_test_data"
        if importlib.util.find_spec(bootstrap_module) is not None:
            print("Running ERPNext bootstrap test data setup")
            frappe.get_module(bootstrap_module)

    test_module = frappe.get_module(f"{args.app}.tests")
    for doctype in getattr(test_module, "global_test_dependencies", ()):
        print(f"Creating global test dependency: {doctype}")
        make_test_records(doctype, commit=True)

    # Persist the one-time global test setup into the reusable database snapshot consumed by all shards.
    frappe.db.commit()  # nosemgrep


if __name__ == "__main__":
    main()
