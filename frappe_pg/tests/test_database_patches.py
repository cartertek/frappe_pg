import inspect
import unittest

from frappe.database.postgres.database import PostgresDatabase, modify_query
from frappe.database.utils import EmptyQueryValues

from frappe_pg.postgres import database_patches
from frappe_pg.postgres.query_transformers import (
    apply_all_query_transformations,
    convert_date_format,
    convert_if_to_case,
    convert_ifnull_to_coalesce,
    convert_mysql_double_quoted_literals,
    convert_mysql_update_join,
    convert_numeric_truthiness,
    normalize_hrms_employee_event_date_parts,
    normalize_hrms_income_tax_salary_slip_grouping,
    normalize_hrms_legacy_string_literals,
    normalize_hrms_reserved_user_alias,
    normalize_hrms_shift_assignment_empty_end_date,
    normalize_hrms_shift_attendance_grouping,
    normalize_hrms_skill_assessment_group_order,
    normalize_hrms_staffing_plan_aggregate,
    normalize_hrms_work_anniversary_date_projection,
    remove_index_hints,
)


class TestQueryTransformers(unittest.TestCase):
    def test_remove_all_index_hint_variants(self):
        cases = {
            "SELECT * FROM `tabGL Entry` FORCE INDEX (posting_date)": "SELECT * FROM `tabGL Entry`",
            "SELECT * FROM `tabGL Entry` use index(idx_a, idx_b)": "SELECT * FROM `tabGL Entry`",
            "SELECT * FROM `tabGL Entry` IGNORE   INDEX (idx_name) WHERE name='x'": "SELECT * FROM `tabGL Entry` WHERE name='x'",
            "SELECT * FROM t FORCE INDEX (a) JOIN u USE INDEX (b) ON t.id=u.id": "SELECT * FROM t JOIN u ON t.id=u.id",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(remove_index_hints(query), expected)

    def test_if_conversion_covers_nested_functions_strings_and_multiple_calls(self):
        cases = {
            "SELECT IF(a > 0, 1, 0)": "SELECT CASE WHEN a > 0 THEN 1 ELSE 0 END",
            "SELECT SUM(IF(status='Active', amount, 0))": "SELECT SUM(CASE WHEN status='Active' THEN amount ELSE 0 END)",
            "SELECT IF(a, CONCAT('x,y', b), COALESCE(c, 0))": "SELECT CASE WHEN a THEN CONCAT('x,y', b) ELSE COALESCE(c, 0) END",
            "SELECT IF(a, IF(b, 1, 2), 3)": "SELECT CASE WHEN a THEN CASE WHEN b THEN 1 ELSE 2 END ELSE 3 END",
            "SELECT IF(a, 1, 0), IF(b, 2, 3)": "SELECT CASE WHEN a THEN 1 ELSE 0 END, CASE WHEN b THEN 2 ELSE 3 END",
            "SELECT DIFF(a, b), IF(flag, 'yes', 'no')": "SELECT DIFF(a, b), CASE WHEN flag THEN 'yes' ELSE 'no' END",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(convert_if_to_case(query), expected)

    def test_ifnull_conversion_is_case_insensitive_and_repeatable(self):
        query = "SELECT IFNULL(a, 0), ifnull(IFNULL(b, 1), 2)"
        expected = "SELECT COALESCE(a, 0), COALESCE(COALESCE(b, 1), 2)"
        self.assertEqual(convert_ifnull_to_coalesce(query), expected)
        self.assertEqual(convert_ifnull_to_coalesce(expected), expected)

    def test_date_format_supported_shape(self):
        cases = {
            "SELECT DATE_FORMAT(posting_date, '%Y-%m-%d')": "SELECT TO_CHAR(posting_date, 'YYYY-MM-DD')",
            'SELECT date_format( posting_date , "%Y-%m-%d" )': "SELECT TO_CHAR(posting_date, 'YYYY-MM-DD')",
            "SELECT DATE_FORMAT(created_at, '%H:%i')": "SELECT DATE_FORMAT(created_at, '%H:%i')",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(convert_date_format(query), expected)

    def test_numeric_truthiness_from_hrms_employee_advance_patch(self):
        query = (
            'UPDATE "tabEmployee Advance" SET "status"=\'Returned\' '
            'WHERE "docstatus"= \'1\' AND "return_amount" '
            'AND "paid_amount"="return_amount" AND "status"=\'Paid\''
        )
        expected = (
            'UPDATE "tabEmployee Advance" SET "status"=\'Returned\' '
            'WHERE "docstatus"= \'1\' AND ("return_amount" <> 0) '
            'AND "paid_amount"="return_amount" AND "status"=\'Paid\''
        )
        self.assertEqual(convert_numeric_truthiness(query), expected)

        nested = (
            'WHERE "docstatus"=1 AND ("claimed_amount" AND "return_amount") '
            'AND "paid_amount"=("return_amount"+"claimed_amount")'
        )
        nested_expected = (
            'WHERE "docstatus"=1 AND (("claimed_amount" <> 0) AND ("return_amount" <> 0)) '
            'AND "paid_amount"=("return_amount"+"claimed_amount")'
        )
        self.assertEqual(convert_numeric_truthiness(nested), nested_expected)

    def test_numeric_truthiness_does_not_touch_compared_identifiers(self):
        query = 'WHERE "paid_amount"="return_amount" AND "docstatus"=1'
        self.assertEqual(convert_numeric_truthiness(query), query)

    def test_numeric_truthiness_does_not_touch_function_arguments(self):
        query = 'SELECT MAX(CHAR_LENGTH("name")) FROM "tabDocField"'
        self.assertEqual(convert_numeric_truthiness(query), query)

    def test_numeric_truthiness_does_not_rewrite_between_upper_bound(self):
        query = (
            'SELECT * FROM "tabLeave Ledger Entry" WHERE '
            '"tabLeave Ledger Entry"."to_date" BETWEEN "tabLeave Allocation"."from_date" '
            'AND "tabLeave Allocation"."to_date" OR "tabLeave Ledger Entry"."is_lwp"'
        )
        expected = (
            'SELECT * FROM "tabLeave Ledger Entry" WHERE '
            '"tabLeave Ledger Entry"."to_date" BETWEEN "tabLeave Allocation"."from_date" '
            'AND "tabLeave Allocation"."to_date" OR ("tabLeave Ledger Entry"."is_lwp" <> 0)'
        )
        self.assertEqual(convert_numeric_truthiness(query), expected)

    def test_numeric_truthiness_still_rewrites_boolean_and(self):
        query = 'SELECT * FROM "tabX" WHERE "tabX"."a" AND "tabX"."b"'
        expected = 'SELECT * FROM "tabX" WHERE ("tabX"."a" <> 0) AND ("tabX"."b" <> 0)'
        self.assertEqual(convert_numeric_truthiness(query), expected)

    def test_hrms_employee_advance_approved_literal_is_quoted_for_postgres(self):
        query = """SELECT sum(ifnull(allocated_amount, 0))
FROM "tabExpense Claim Advance" eca, "tabExpense Claim" ec
WHERE eca.employee_advance=%s AND ec.approval_status="Approved" AND ec.name=eca.parent"""
        transformed = normalize_hrms_legacy_string_literals(query)
        self.assertIn("ec.approval_status='Approved'", transformed)

    def test_hrms_salary_detail_earnings_literal_is_quoted_for_postgres(self):
        query = (
            'select sum(sd.amount) from "tabSalary Slip" ss, "tabSalary Detail" sd\n'
            'where ss.name=sd.parent and sd.parentfield = "earnings"'
        )
        transformed = normalize_hrms_legacy_string_literals(query)
        self.assertIn("sd.parentfield='earnings'", transformed)

    def test_hrms_employee_reminder_alias_uses_identifier_quotes(self):
        query = (
            "SELECT \"personal_email\", \"employee_name\" AS 'name', \"image\" "
            "FROM \"tabEmployee\" WHERE \"status\"='Active'"
        )
        transformed = normalize_hrms_legacy_string_literals(query)
        self.assertIn('employee_name" AS "name"', transformed)
        self.assertNotIn("AS 'name'", transformed)

    def test_hrms_benefit_claim_aggregate_alias_uses_identifier_quotes(self):
        query = (
            "select sum(claimed_amount) as 'total_amount' "
            '\nfrom "tabEmployee Benefit Claim" where employee=%(employee)s'
        )
        transformed = normalize_hrms_legacy_string_literals(query)
        self.assertIn('AS "total_amount"', transformed)
        self.assertNotIn("AS 'total_amount'", transformed)

    def test_hrms_employee_event_date_parts_cast_today(self):
        query = (
            'SELECT "employee_name" FROM "tabEmployee" WHERE '
            "DATE_PART('day', date_of_birth) = date_part('day', %(today)s) AND "
            "DATE_PART('month', date_of_birth) = date_part('month', %(today)s) AND "
            "DATE_PART('year', date_of_birth) < date_part('year', %(today)s)"
        )
        transformed = normalize_hrms_employee_event_date_parts(query)
        self.assertEqual(transformed.count('CAST(%(today)s AS date)'), 3)

    def test_hrms_work_anniversary_projects_date_of_joining(self):
        query = (
            'SELECT "personal_email", "company", "company_email", "user_id", '
            '"employee_name" AS "name", "image" FROM "tabEmployee" WHERE '
            "DATE_PART('day', date_of_joining) = date_part('day', %(today)s)"
        )
        transformed = normalize_hrms_work_anniversary_date_projection(query)
        self.assertIn('"image", "date_of_joining" FROM "tabEmployee"', transformed)
        self.assertEqual(normalize_hrms_work_anniversary_date_projection(transformed), transformed)

    def test_hrms_staffing_plan_aggregate_adds_group_by(self):
        query = """SELECT DISTINCT spd.parent, sp.from_date as from_date, sp.to_date as to_date, sp.name,
sum(spd.vacancies) as vacancies, spd.designation
FROM "tabStaffing Plan Detail" spd, "tabStaffing Plan" sp WHERE spd.parent=sp.name"""
        transformed = normalize_hrms_staffing_plan_aggregate(query)
        self.assertIn("GROUP BY spd.parent, sp.from_date, sp.to_date, sp.name, spd.designation", transformed)

    def test_hrms_income_tax_salary_slip_name_is_aggregated(self):
        query = (
            'SELECT "tabSalary Slip"."name","tabSalary Slip"."employee",\n'
            '"tabSalary Detail"."salary_component",SUM("tabSalary Detail"."amount") "amount"\n'
            'FROM "tabSalary Slip" INNER JOIN "tabSalary Detail" '
            'ON "tabSalary Slip"."name"="tabSalary Detail"."parent"\n'
            'GROUP BY "tabSalary Slip"."employee","tabSalary Detail"."salary_component"'
        )
        transformed = normalize_hrms_income_tax_salary_slip_grouping(query)
        self.assertIn('MIN("tabSalary Slip"."name") AS "name"', transformed)

    def test_unrelated_double_quoted_identifier_is_unchanged(self):
        query = 'SELECT * FROM "tabOther" WHERE status="Approved"'
        self.assertEqual(normalize_hrms_legacy_string_literals(query), query)

    def test_hrms_shift_assignment_empty_end_date_becomes_null(self):
        query = (
            'SELECT "employee" FROM "tabShift Assignment" WHERE '
            "(\"end_date\">=%(date)s OR \"end_date\" IS NULL OR \"end_date\"='')"
        )
        transformed = normalize_hrms_shift_assignment_empty_end_date(query)
        self.assertNotIn('"end_date"=', transformed)
        self.assertIn('"end_date" IS NULL', transformed)

    def test_hrms_shift_assignment_empty_end_date_parameter_becomes_null(self):
        query = (
            'SELECT "employee" FROM "tabShift Assignment" WHERE '
            '("end_date">=%(date)s OR "end_date" IS NULL OR "end_date"=%(param4)s)'
        )
        transformed = normalize_hrms_shift_assignment_empty_end_date(query)
        self.assertNotIn('"end_date"=%(param4)s', transformed)
        self.assertIn('"end_date" IS NULL', transformed)

    def test_other_empty_string_comparison_is_unchanged(self):
        query = "SELECT \"name\" FROM \"tabOther\" WHERE \"end_date\"=''"
        self.assertEqual(normalize_hrms_shift_assignment_empty_end_date(query), query)

    def test_hrms_skill_assessment_group_order_uses_min_idx(self):
        query = (
            'SELECT "tabSkill Assessment"."skill",'
            'AVG("tabSkill Assessment"."rating") "rating" '
            'FROM "tabSkill Assessment" JOIN "tabInterview Feedback" ON 1=1 '
            'GROUP BY "tabSkill Assessment"."skill" '
            'ORDER BY "tabSkill Assessment"."idx"'
        )
        transformed = normalize_hrms_skill_assessment_group_order(query)
        self.assertIn('ORDER BY MIN("tabSkill Assessment"."idx")', transformed)

    def test_double_quoted_mysql_string_literal_with_spaces(self):
        query = 'SELECT * FROM "tabSingles" WHERE doctype = "HR Settings" AND field = \'x\''
        expected = 'SELECT * FROM "tabSingles" WHERE doctype = \'HR Settings\' AND field = \'x\''
        self.assertEqual(convert_mysql_double_quoted_literals(query), expected)

    def test_double_quoted_mysql_like_pattern(self):
        query = 'select data from "__UserSettings" where data like "%%%s%%"'
        expected = "select data from \"__UserSettings\" where data like '%%' || %s || '%%'"
        self.assertEqual(convert_mysql_double_quoted_literals(query), expected)

    def test_double_quoted_mysql_like_literal_without_placeholder(self):
        query = 'select name from "tabExample" where name like "prefix%"'
        expected = "select name from \"tabExample\" where name like 'prefix%'"
        self.assertEqual(convert_mysql_double_quoted_literals(query), expected)

    def test_double_quoted_mysql_string_literals_in_legacy_in_list(self):
        query = (
            'SELECT * FROM "tabSingles" WHERE field in ('
            '"encrypt_salary_slips_in_emails", "email_salary_slip_to_employee", "password_policy")'
        )
        expected = (
            'SELECT * FROM "tabSingles" WHERE field in ('
            "'encrypt_salary_slips_in_emails', 'email_salary_slip_to_employee', 'password_policy')"
        )
        self.assertEqual(convert_mysql_double_quoted_literals(query), expected)

    def test_quoted_identifier_in_list_is_not_rewritten(self):
        query = 'SELECT * FROM "tabExample" WHERE "name" IN ("other_column", "another_column")'
        self.assertEqual(convert_mysql_double_quoted_literals(query), query)

    def test_double_quoted_qualified_identifier_with_spaces_is_unchanged(self):
        query = (
            'SELECT "tabWeb Page"."route" FROM "tabWeb Page" '
            'LEFT JOIN "tabWeb Page Block" ON '
            '"tabWeb Page Block"."parent"="tabWeb Page"."name"'
        )
        self.assertEqual(convert_mysql_double_quoted_literals(query), query)

    def test_double_quoted_identifier_rhs_is_not_rewritten(self):
        query = 'SELECT * FROM "tabEmployee Advance" WHERE "paid_amount" = "return_amount"'
        self.assertEqual(convert_mysql_double_quoted_literals(query), query)

    def test_hrms_reserved_user_alias_is_quoted(self):
        query = (
            'SELECT DISTINCT(has_role.parent) FROM "tabHas Role" has_role '
            'LEFT JOIN "tabUser" user ON has_role.parent = user.name '
            "WHERE has_role.parenttype = 'User' AND user.enabled = '1'"
        )
        transformed = normalize_hrms_reserved_user_alias(query)
        self.assertIn('LEFT JOIN "tabUser" "user"', transformed)
        self.assertIn('"user".name', transformed)
        self.assertIn('"user".enabled', transformed)

    def test_hrms_shift_attendance_joined_values_are_aggregated(self):
        query = (
            'SELECT "tabAttendance"."name","tabEmployee Checkin"."shift_start",'
            '"tabEmployee Checkin"."shift_end","tabShift Type"."enable_late_entry_marking" '
            'FROM "tabAttendance" JOIN "tabShift Type" ON 1=1 '
            'JOIN "tabEmployee Checkin" ON 1=1 GROUP BY "tabAttendance"."name"'
        )
        transformed = normalize_hrms_shift_attendance_grouping(query)
        self.assertIn('MAX("tabEmployee Checkin"."shift_start") AS "shift_start"', transformed)
        self.assertIn('MAX("tabEmployee Checkin"."shift_end") AS "shift_end"', transformed)
        self.assertIn(
            'MAX("tabShift Type"."enable_late_entry_marking") AS "enable_late_entry_marking"', transformed
        )

    def test_simple_mysql_update_join(self):
        query = (
            'UPDATE "tabSalary Detail" "sd" JOIN "tabSalary Structure" "ss" '
            'ON "ss"."name"="sd"."parent" SET "sd"."docstatus"= \'1\' '
            'WHERE "ss"."docstatus"= \'1\' AND "sd"."parenttype"=%(param1)s'
        )
        expected = (
            'UPDATE "tabSalary Detail" AS "sd" SET "docstatus"= \'1\' '
            'FROM "tabSalary Structure" AS "ss" '
            'WHERE "ss"."name"="sd"."parent" AND '
            '"ss"."docstatus"= \'1\' AND "sd"."parenttype"=%(param1)s'
        )
        self.assertEqual(convert_mysql_update_join(query), expected)

    def test_complex_update_join_is_left_unchanged(self):
        query = (
            'UPDATE "a" "a1" JOIN "b" "b1" ON "a1"."id"="b1"."id" '
            'JOIN "c" "c1" ON "b1"."id"="c1"."id" SET "a1"."x"=1 WHERE "c1"."y"=2'
        )
        self.assertEqual(convert_mysql_update_join(query), query)

    def test_pipeline_is_idempotent_for_supported_transformations(self):
        queries = [
            "SELECT IFNULL(IF(a > 0, a, 0), 0)",
            "SELECT DATE_FORMAT(posting_date, '%Y-%m-%d') FROM `tabGL Entry` FORCE INDEX (posting_date)",
            "SELECT IF(a, DATE_FORMAT(d, '%Y-%m-%d'), IFNULL(x, 'n/a')) FROM t USE INDEX (idx_a)",
        ]
        for query in queries:
            with self.subTest(query=query):
                once = apply_all_query_transformations(query)
                self.assertEqual(apply_all_query_transformations(once), once)


class TestTransformQueryHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_patches.apply_postgres_fixes()

    def test_postgres_execution_and_transaction_methods_are_not_replaced(self):
        self.assertEqual(PostgresDatabase.sql.__name__, "sql")
        self.assertEqual(PostgresDatabase.sql.__module__, "frappe.database.postgres.database")
        self.assertEqual(PostgresDatabase.commit.__module__, "frappe.database.database")
        self.assertEqual(PostgresDatabase.rollback.__module__, "frappe.database.database")
        self.assertNotEqual(PostgresDatabase._transform_query.__module__, PostgresDatabase.sql.__module__)
        self.assertIs(PostgresDatabase._transform_query, database_patches.patched_transform_query)

    def test_native_sql_default_still_uses_empty_query_values(self):
        default = inspect.signature(PostgresDatabase.sql).parameters["values"].default
        self.assertEqual(default, EmptyQueryValues)

    def test_transform_hook_preserves_values_exactly(self):
        values_cases = [
            EmptyQueryValues,
            (),
            (1, "x"),
            [1, "x"],
            {"name": "x", "count": 1},
            "scalar",
        ]
        db = object.__new__(PostgresDatabase)
        for values in values_cases:
            with self.subTest(values=values):
                _, transformed_values = database_patches.patched_transform_query(
                    db, "SELECT IF(1, 2, 3)", values
                )
                if values is EmptyQueryValues or isinstance(values, list | dict):
                    self.assertIs(transformed_values, values)
                else:
                    self.assertEqual(transformed_values, values)

    def test_literal_percent_schema_query_regression(self):
        query = """
            SELECT a.column_name,
                   indexdef LIKE '%UNIQUE INDEX%' AS unique,
                   indexdef NOT LIKE '%UNIQUE INDEX%' AS index
            FROM information_schema.columns a
            LEFT JOIN pg_indexes b
              ON SUBSTRING(b.indexdef, '(.*)') LIKE CONCAT('%', a.column_name, '%')
        """
        db = object.__new__(PostgresDatabase)
        transformed_query, transformed_values = database_patches.patched_transform_query(
            db, query, EmptyQueryValues
        )
        self.assertEqual(transformed_query, query)
        self.assertEqual(transformed_values, EmptyQueryValues)

    def test_transform_order_matches_legacy_pipeline_for_supported_corpus(self):
        """Prove post-modify transformation matches the old pre-modify ordering."""
        corpus = [
            "SELECT * FROM `tabGL Entry` FORCE INDEX (posting_date)",
            "SELECT * FROM `tabGL Entry` USE INDEX (posting_date) WHERE posting_date >= '2024-01-01'",
            "SELECT * FROM `tabGL Entry` IGNORE INDEX (name) WHERE docstatus = 1",
            "SELECT IF(amount > 0, amount, 0) FROM `tabGL Entry`",
            "SELECT SUM(IF(docstatus = 1, debit, credit)) FROM `tabGL Entry`",
            "SELECT IF(a, IF(b, 1, 2), IF(c, 3, 4)) FROM `tabTest`",
            "SELECT IFNULL(name, 'N/A') FROM `tabItem`",
            "SELECT IFNULL(IF(amount > 0, amount, 0), 0) FROM `tabGL Entry`",
            "SELECT DATE_FORMAT(posting_date, '%Y-%m-%d') FROM `tabGL Entry`",
            "SELECT IF(DATE_FORMAT(posting_date, '%Y-%m-%d')='2024-01-01', IFNULL(name, 'x'), 'y') FROM `tabGL Entry` FORCE INDEX (posting_date)",
            "SELECT IF(LOCATE('x', name) > 0, 1, 0) FROM `tabItem`",
            "SELECT IF(name REGEXP '^A', 1, 0) FROM `tabItem`",
            "SELECT IF(a > -45.0, 1, 0) FROM `tabTest`",
            "SELECT IF(a > 45, 1, 0) FROM `tabTest`",
            "SELECT IF(a, CONCAT('x,y', b), 0) FROM `tabTest`",
        ]
        for query in corpus:
            with self.subTest(query=query):
                legacy_order = modify_query(apply_all_query_transformations(query))
                transform_hook_order = apply_all_query_transformations(modify_query(query))
                self.assertEqual(transform_hook_order, legacy_order)

    def test_patch_application_is_idempotent(self):
        transform = PostgresDatabase._transform_query
        database_patches.apply_postgres_fixes()
        self.assertIs(PostgresDatabase._transform_query, transform)

    def test_patch_can_be_safely_removed_and_reapplied(self):
        database_patches.remove_postgres_fixes()
        self.assertIsNot(PostgresDatabase._transform_query, database_patches.patched_transform_query)
        database_patches.apply_postgres_fixes()
        self.assertIs(PostgresDatabase._transform_query, database_patches.patched_transform_query)
        db = object.__new__(PostgresDatabase)
        query, values = database_patches.patched_transform_query(db, "SELECT IF(1, 2, 3)", EmptyQueryValues)
        self.assertEqual(query, "SELECT CASE WHEN 1 THEN 2 ELSE 3 END")
        self.assertEqual(values, EmptyQueryValues)

    def test_status_reports_transform_hook_and_unmodified_sql(self):
        status = database_patches.check_patches_status()
        self.assertTrue(status["patches_applied"])
        self.assertTrue(status["transform_query_patched"])
        self.assertFalse(status["sql_patched"])
        self.assertFalse(status["commit_patched"])
        self.assertFalse(status["rollback_patched"])
