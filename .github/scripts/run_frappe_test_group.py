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
    # test_seen relies on roles added by test_permissions and fails in isolation.
    # Keep them together, in upstream order, so sharding does not expose that
    # hidden test-order dependency as a PostgreSQL failure.
    "permissions": ["tests/test_permissions.py", "tests/test_seen.py"],
    "db-query": ["tests/test_db_query.py"],
    "auth": ["tests/test_auth.py"],
    "boot": ["tests/test_boot.py"],
    "fixture-import": ["tests/test_fixture_import.py"],
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


def patch_v15_fixture_import_test(module):
    """Backport v16's fixture-import transaction ordering to the v15 test."""
    if module.__name__ != "frappe.tests.test_fixture_import":
        return
    test_class = getattr(module, "TestFixtureImport", None)
    if test_class is None:
        return
    method = test_class.test_fixtures_import
    if getattr(method, "_frappe_pg_fixture_guard", False) or "frappe.db.commit()" in inspect.getsource(
        method
    ):
        return

    def compatible_test(self):
        self.assertFalse(frappe.db.exists("DocType", "temp_doctype"))
        self.create_new_doctype("temp_doctype")
        frappe.db.commit()  # v16 commits the newly created DocType before fixture DML; nosemgrep

        dummy_names = ["jhon", "jane"]
        path = self.insert_dummy_data_and_export("temp_doctype", dummy_names)
        frappe.db.truncate("temp_doctype")
        module.import_doc(path)

        self.assertEqual(frappe.db.count("temp_doctype"), len(dummy_names))
        data = frappe.get_all("temp_doctype", "member_name")
        frappe.db.truncate("temp_doctype")
        self.assertEqual(set(dummy_names), {row["member_name"] for row in data})

        module.delete_doc("DocType", "temp_doctype", delete_permanently=True)
        frappe.db.commit()  # v16 commits permanent DocType deletion before removing the exported fixture; nosemgrep
        module.os.remove(path)

    compatible_test._frappe_pg_fixture_guard = True
    test_class.test_fixtures_import = compatible_test


def patch_v15_doctype_delete_cache():
    """Backport v16's DocType cache invalidation behavior for v15 tests."""
    from frappe.model import delete_doc as delete_doc_module

    original = delete_doc_module.delete_doc
    try:
        source = inspect.getsource(original)
    except (OSError, TypeError):
        return
    if "frappe.clear_cache(doctype=name)" in source or getattr(original, "_frappe_pg_cache_guard", False):
        return

    def delete_doc_with_cache(doctype, name, *args, **kwargs):
        parent_doctypes = []
        if doctype == "DocType":
            parent_doctypes = frappe.get_all(
                "Custom Field",
                filters={"options": name, "fieldtype": ["in", frappe.model.table_fields]},
                pluck="dt",
            )

        result = original(doctype, name, *args, **kwargs)
        if doctype == "DocType":
            frappe.clear_cache(doctype=name)
            for parent in parent_doctypes:
                frappe.clear_cache(doctype=parent)
        return result

    delete_doc_with_cache._frappe_pg_cache_guard = True
    delete_doc_module.delete_doc = delete_doc_with_cache
    frappe.delete_doc = delete_doc_with_cache


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
            patch_v15_fixture_import_test(module)
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
                    raise RuntimeError(
                        f"Missing expected Frappe test file alternatives: {', '.join(requested)}"
                    )
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
    patch_v15_doctype_delete_cache()
    runner = SelectedTestRunner(site=args.site, group=args.group)
    # v15 runs during ParallelTestRunner.__init__; v16+ separates construction
    # from execution behind setup_and_run().
    if hasattr(runner, "setup_and_run"):
        runner.setup_and_run()


if __name__ == "__main__":
    main()
