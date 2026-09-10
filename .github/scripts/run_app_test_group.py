#!/usr/bin/env python3
"""Run a complete ERPNext/HRMS test suite in deterministic CI shards.

Heavy/stateful modules measured from full-suite PostgreSQL runs are kept
intact. Every other discovered test file belongs to exactly one weighted
remainder shard; validation fails if a file is lost or duplicated.
"""

import argparse
import os

import frappe
from frappe.parallel_test_runner import ParallelTestRunner, get_all_tests, split_by_weight

ISOLATED_GROUPS = {
    "erpnext": {
        # ~144s of slow-test time by itself on the v15 PostgreSQL run. Keep the
        # accounting reconciliation tests together because they share extensive
        # payment-ledger setup/state.
        "payment-reconciliation": [
            "accounts/doctype/payment_reconciliation/test_payment_reconciliation.py",
        ],
    },
    "hrms": {
        # These related leave modules accounted for ~273s of slow-test time on
        # the v15 PostgreSQL run and share leave-policy/allocation fixtures.
        "leave-heavy": [
            "hr/doctype/leave_allocation/test_earned_leaves.py",
            "hr/doctype/leave_allocation/test_leave_allocation.py",
            "hr/doctype/leave_application/test_leave_application.py",
            "hr/doctype/leave_encashment/test_leave_encashment.py",
        ],
        # Payroll Entry + Salary Slip accounted for ~157s of slow-test time and
        # exercise the same payroll setup, so preserve their relative ordering.
        "payroll-heavy": [
            "payroll/doctype/payroll_entry/test_payroll_entry.py",
            "payroll/doctype/salary_slip/test_salary_slip.py",
        ],
    },
}

REMAINDER_COUNTS = {"erpnext": 10, "hrms": 6}


def relative_test_path(app, test_file):
    path, filename = test_file
    return os.path.relpath(os.path.join(path, filename), frappe.get_app_path(app)).replace(os.sep, "/")


def unique_tests_by_path(app, tests):
    by_path = {}
    for test in tests:
        by_path.setdefault(relative_test_path(app, test), test)
    return by_path


def isolated_paths(app):
    return {
        path
        for paths in ISOLATED_GROUPS.get(app, {}).values()
        for path in paths
    }


def validate_chunks(app, remainder, chunks):
    remainder_paths = {relative_test_path(app, test) for test in remainder}
    chunk_paths = [{relative_test_path(app, test) for test in chunk} for chunk in chunks]

    seen = set()
    overlap = set()
    for paths in chunk_paths:
        overlap.update(seen & paths)
        seen.update(paths)
    if overlap:
        raise RuntimeError(f"{app} remainder shards overlap: {', '.join(sorted(overlap))}")

    if seen != remainder_paths:
        missing = remainder_paths - seen
        extra = seen - remainder_paths
        details = []
        if missing:
            details.append(f"missing: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"extra: {', '.join(sorted(extra))}")
        raise RuntimeError(f"{app} remainder shards do not exactly cover the remainder ({'; '.join(details)})")


class SelectedAppTestRunner(ParallelTestRunner):
    def __init__(self, app, site, group):
        self.group = group
        self.selected_app = app
        super().__init__(app, site=site)

    def get_test_file_list(self):
        app = self.selected_app
        by_path = unique_tests_by_path(app, get_all_tests(app))
        tests = [by_path[path] for path in sorted(by_path)]

        groups = ISOLATED_GROUPS.get(app, {})
        if self.group in groups:
            requested = groups[self.group]
            missing = [path for path in requested if path not in by_path]
            if missing:
                raise RuntimeError(f"Missing expected {app} test files: {', '.join(missing)}")
            selected = [by_path[path] for path in requested]
            print(f"Selected {len(selected)} {app} test files for {self.group}:")
            for test in selected:
                print(f"  {relative_test_path(app, test)}")
            return selected

        remainder = [test for test in tests if relative_test_path(app, test) not in isolated_paths(app)]
        shard_count = REMAINDER_COUNTS[app]
        expected_groups = {f"remainder-{index}" for index in range(1, shard_count + 1)}
        if self.group not in expected_groups:
            raise RuntimeError(f"Unknown {app} test group: {self.group}")

        weight_fn = getattr(self, "get_test_weight", None) or self.get_test_count
        weights = [weight_fn(test) for test in remainder]
        chunks = split_by_weight(remainder, weights, chunk_count=shard_count)
        validate_chunks(app, remainder, chunks)
        selected = chunks[int(self.group.rsplit("-", 1)[1]) - 1]
        print(f"Selected {len(selected)} {app} test files for {self.group}:")
        for test in selected:
            print(f"  {relative_test_path(app, test)}")
        return selected


def initialize_frappe_pg_runtime():
    """Reapply frappe_pg runtime compatibility in each fresh shard process."""
    from frappe_pg.postgres.database_patches import apply_postgres_fixes
    from frappe_pg.compat.registry import apply_compatibility_overrides

    apply_postgres_fixes()
    apply_compatibility_overrides()



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--app", choices=sorted(REMAINDER_COUNTS), required=True)
    parser.add_argument("--group", required=True)
    args = parser.parse_args()

    print(f"Running {args.app} test group {args.group}")
    initialize_frappe_pg_runtime()
    runner = SelectedAppTestRunner(app=args.app, site=args.site, group=args.group)
    # v15 runs during ParallelTestRunner.__init__; v16+ separates construction
    # from execution behind setup_and_run().
    if hasattr(runner, "setup_and_run"):
        runner.setup_and_run()


if __name__ == "__main__":
    main()
