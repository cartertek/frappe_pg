#!/usr/bin/env python3
import argparse
import os

import frappe
from frappe.parallel_test_runner import ParallelTestRunner, get_all_tests, split_by_weight

ISOLATED_GROUPS = {
    "webhook": ["integrations/doctype/webhook/test_webhook.py"],
    "rq": [
        "core/doctype/rq_job/test_rq_job.py",
        "core/doctype/rq_worker/test_rq_worker.py",
    ],
    "permissions": ["tests/test_permissions.py"],
    "db-query": ["tests/test_db_query.py"],
    "commands": ["commands/test_commands.py"],
    "user-invitation": ["core/doctype/user_invitation/test_user_invitation.py"],
}
REMAINDER_GROUPS = {"remainder", "remainder-1", "remainder-2"}


def relative_test_path(test_file):
    path, filename = test_file
    return os.path.relpath(os.path.join(path, filename), frappe.get_app_path("frappe")).replace(os.sep, "/")


def isolated_paths():
    return {path for paths in ISOLATED_GROUPS.values() for path in paths}


class SelectedTestRunner(ParallelTestRunner):
    def __init__(self, site, group):
        self.group = group
        super().__init__("frappe", site=site)

    def get_test_file_list(self):
        tests = get_all_tests("frappe")
        by_path = {relative_test_path(test): test for test in tests}

        if self.group in ISOLATED_GROUPS:
            requested = ISOLATED_GROUPS[self.group]
            missing = [path for path in requested if path not in by_path]
            if missing:
                raise RuntimeError(f"Missing expected Frappe test files: {', '.join(missing)}")
            return [by_path[path] for path in requested]

        remainder = [test for test in tests if relative_test_path(test) not in isolated_paths()]
        if self.group == "remainder":
            return remainder

        weight_fn = getattr(self, "get_test_weight", None) or self.get_test_count
        weights = [weight_fn(test) for test in remainder]
        chunks = split_by_weight(remainder, weights, chunk_count=2)
        return chunks[int(self.group.rsplit("-", 1)[1]) - 1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--group", choices=[*ISOLATED_GROUPS, *REMAINDER_GROUPS], required=True)
    args = parser.parse_args()

    print(f"Running Frappe test group {args.group}")
    runner = SelectedTestRunner(site=args.site, group=args.group)
    # v15 runs during ParallelTestRunner.__init__; v16+ separates construction
    # from execution behind setup_and_run().
    if hasattr(runner, "setup_and_run"):
        runner.setup_and_run()


if __name__ == "__main__":
    main()
