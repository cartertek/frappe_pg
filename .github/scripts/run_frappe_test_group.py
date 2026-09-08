#!/usr/bin/env python3
import argparse
import inspect
import os
import time
from contextlib import contextmanager

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
    "auth": ["tests/test_auth.py"],
    "seen": ["tests/test_seen.py"],
    "email-account": ["email/doctype/email_account/test_email_account.py"],
    # Frappe moved this module between v15 and v16.
    "commands": ["tests/test_commands.py", "commands/test_commands.py"],
}
ALTERNATIVE_PATH_GROUPS = {"commands"}
REMAINDER_GROUPS = {"remainder", "remainder-1", "remainder-2"}

# These files each contain a single assertion whose premise is that Frappe is
# the only installed app. That premise is intentionally false here because
# frappe_pg must be installed to exercise PostgreSQL compatibility.
EXCLUDED_TEST_PATHS = {
    "core/doctype/installed_applications/test_installed_applications.py",
    "custom/report/audit_system_hooks/test_audit_system_hooks.py",
}


def relative_test_path(test_file):
    path, filename = test_file
    return os.path.relpath(os.path.join(path, filename), frappe.get_app_path("frappe")).replace(os.sep, "/")


def isolated_paths():
    return {path for paths in ISOLATED_GROUPS.values() for path in paths}


def unique_tests_by_path(tests):
    """Collapse duplicate discoveries before splitting the remainder."""
    by_path = {}
    for test in tests:
        by_path.setdefault(relative_test_path(test), test)
    return by_path


def patch_v15_query_count_helper():
    """Backport v16's string conversion for psycopg query objects in v15 tests."""
    try:
        from frappe.tests.utils import FrappeTestCase
    except ImportError:
        return

    source = inspect.getsource(FrappeTestCase.assertQueryCount)
    if "str(args[0].last_query)" in source:
        return

    @contextmanager
    def assert_query_count(self, count):
        queries = []
        orig_sql = frappe.db.__class__.sql

        def sql_with_count(*args, **kwargs):
            result = orig_sql(*args, **kwargs)
            queries.append(str(args[0].last_query))
            return result

        try:
            frappe.db.__class__.sql = sql_with_count
            yield
            self.assertLessEqual(len(queries), count, msg="Queries executed: \n" + "\n\n".join(queries))
        finally:
            frappe.db.__class__.sql = orig_sql

    FrappeTestCase.assertQueryCount = assert_query_count


def skip_v15_stale_assertions(module):
    """Skip assertions already stale against the v15 implementation itself."""
    if module.__name__ != "frappe.tests.test_db":
        return
    test_class = getattr(module, "TestDDLCommandsPost", None)
    if test_class is None:
        return
    method = getattr(test_class, "test_is", None)
    if method is None or "coalesce" not in inspect.getsource(method).lower():
        return
    method.__unittest_skip__ = True
    method.__unittest_skip_why__ = "v15 assertion still expects COALESCE after Frappe removed it from func_is"


def patch_v15_command_test(module):
    """Backport the v16 guard against same-second backup filename collisions."""
    if module.__name__ != "frappe.tests.test_commands":
        return
    test_class = getattr(module, "TestBackups", None)
    if test_class is None:
        return
    method = test_class.test_backup_no_options
    if getattr(method, "_frappe_pg_sleep_guard", False) or "time.sleep(1)" in inspect.getsource(method):
        return

    def guarded_test(self):
        time.sleep(1)
        return method(self)

    guarded_test._frappe_pg_sleep_guard = True
    test_class.test_backup_no_options = guarded_test


def validate_chunks(remainder, chunks):
    remainder_paths = {relative_test_path(test) for test in remainder}
    chunk_paths = [{relative_test_path(test) for test in chunk} for chunk in chunks]

    overlap = chunk_paths[0] & chunk_paths[1]
    if overlap:
        raise RuntimeError(f"Remainder shards overlap: {', '.join(sorted(overlap))}")

    covered = chunk_paths[0] | chunk_paths[1]
    if covered != remainder_paths:
        missing = remainder_paths - covered
        extra = covered - remainder_paths
        details = []
        if missing:
            details.append(f"missing: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"extra: {', '.join(sorted(extra))}")
        raise RuntimeError(f"Remainder shards do not exactly cover the remainder ({'; '.join(details)})")


class SelectedTestRunner(ParallelTestRunner):
    def __init__(self, site, group):
        self.group = group
        super().__init__("frappe", site=site)

    def run_tests_for_file(self, file_info):
        if file_info:
            path, filename = file_info
            module = self.get_module(path, filename)
            skip_v15_stale_assertions(module)
            patch_v15_command_test(module)
        return super().run_tests_for_file(file_info)

    def get_test_file_list(self):
        by_path = unique_tests_by_path(get_all_tests("frappe"))
        # Each shard runs in a separate CI job, so discovery order must not
        # influence which shard owns a file.
        tests = [by_path[path] for path in sorted(by_path)]

        if self.group in ISOLATED_GROUPS:
            requested = ISOLATED_GROUPS[self.group]
            selected = [by_path[path] for path in requested if path in by_path]
            if self.group in ALTERNATIVE_PATH_GROUPS:
                if not selected:
                    raise RuntimeError(f"Missing expected Frappe test file alternatives: {', '.join(requested)}")
            else:
                missing = [path for path in requested if path not in by_path]
                if missing:
                    raise RuntimeError(f"Missing expected Frappe test files: {', '.join(missing)}")
            return selected

        remainder = [
            test
            for test in tests
            if relative_test_path(test) not in isolated_paths()
            and relative_test_path(test) not in EXCLUDED_TEST_PATHS
        ]
        if self.group == "remainder":
            return remainder

        weight_fn = getattr(self, "get_test_weight", None) or self.get_test_count
        weights = [weight_fn(test) for test in remainder]
        chunks = split_by_weight(remainder, weights, chunk_count=2)
        validate_chunks(remainder, chunks)
        return chunks[int(self.group.rsplit("-", 1)[1]) - 1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--group", choices=[*ISOLATED_GROUPS, *REMAINDER_GROUPS], required=True)
    args = parser.parse_args()

    print(f"Running Frappe test group {args.group}")
    patch_v15_query_count_helper()
    runner = SelectedTestRunner(site=args.site, group=args.group)
    # v15 runs during ParallelTestRunner.__init__; v16+ separates construction
    # from execution behind setup_and_run().
    if hasattr(runner, "setup_and_run"):
        runner.setup_and_run()


if __name__ == "__main__":
    main()
