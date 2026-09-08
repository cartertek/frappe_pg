import inspect
import unittest
from unittest.mock import Mock, patch

import frappe
from frappe.database.postgres.database import PostgresDatabase, modify_query
from frappe.database.utils import EmptyQueryValues

from frappe_pg.postgres import database_patches
from frappe_pg.postgres.query_transformers import (
    apply_all_query_transformations,
    cast_timestamp_pattern_matches,
    convert_date_format,
    convert_if_to_case,
    convert_ifnull_to_coalesce,
    convert_mysql_date_arithmetic,
    convert_mysql_double_quoted_literals,
    convert_mysql_inner_join_without_condition,
    convert_mysql_update_join,
    convert_mysql_zero_date_sentinel,
    convert_numeric_truthiness,
    expand_mysql_having_alias,
    normalize_erpnext_item_end_of_life_zero_date,
    normalize_erpnext_landed_cost_center_aggregate,
    normalize_erpnext_negative_invoice_voucher_literal,
    normalize_erpnext_v15_bom_group_query,
    normalize_payment_request_single_match_grouping,
    remove_erpnext_inventory_dimension_default_order,
    remove_index_hints,
    remove_order_by_from_aggregate_only_query,
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

    def test_mysql_zero_date_sentinel_in_date_shaped_coalesce(self):
        query = (
            "where coalesce(date, '2199-12-31 00:00:00') >= '0' "
            "and coalesce(start_date, '2199-12-31') >= '0.0'"
        )
        expected = (
            "where coalesce(date, '2199-12-31 00:00:00') >= '0001-01-01 00:00:00' "
            "and coalesce(start_date, '2199-12-31') >= '0001-01-01 00:00:00'"
        )
        self.assertEqual(convert_mysql_zero_date_sentinel(query), expected)

    def test_zero_numeric_comparison_is_not_rewritten_as_date(self):
        query = "WHERE COALESCE(amount, 0) >= '0'"
        self.assertEqual(convert_mysql_zero_date_sentinel(query), query)

    def test_mysql_date_sub_curdate_from_erpnext_dashboard(self):
        query = "transaction_date > date_sub(curdate(), interval 1 year)"
        expected = "transaction_date > CURRENT_DATE - INTERVAL '1 year'"
        self.assertEqual(convert_mysql_date_arithmetic(query), expected)

    def test_mysql_date_arithmetic_supported_units_and_case(self):
        cases = {
            "DATE_SUB(posting_date, INTERVAL 30 DAY)": "posting_date - INTERVAL '30 day'",
            "date_sub(posting_date, interval 2 WEEK)": "posting_date - INTERVAL '2 week'",
            "DATE_SUB(posting_date, INTERVAL 3 MONTH)": "posting_date - INTERVAL '3 month'",
            "CURDATE()": "CURRENT_DATE",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(convert_mysql_date_arithmetic(query), expected)

    def test_mysql_date_arithmetic_leaves_unsupported_shapes_unchanged(self):
        cases = [
            "DATE_SUB(posting_date, INTERVAL amount DAY)",
            "DATE_SUB(posting_date, INTERVAL 1 HOUR)",
            "DATE_SUB(COALESCE(posting_date, creation), INTERVAL 1 YEAR)",
        ]
        for query in cases:
            with self.subTest(query=query):
                self.assertEqual(convert_mysql_date_arithmetic(query), query)

    def test_mysql_having_alias_from_frappe_system_health_report(self):
        query = """
            select scheduled_job_type,
                   avg(CASE WHEN status != 'Complete' THEN 1 ELSE 0 END) * 100 as failure_rate
            from "tabScheduled Job Log"
            group by scheduled_job_type
            having failure_rate > '0'
            order by failure_rate desc
        """
        transformed = expand_mysql_having_alias(query)
        self.assertIn(
            "having (avg(CASE WHEN status != 'Complete' THEN 1 ELSE 0 END) * 100) > '0'",
            transformed,
        )
        self.assertIn("order by failure_rate desc", transformed)

    def test_mysql_having_unknown_alias_is_unchanged(self):
        query = "SELECT count(*) AS total FROM tabThing HAVING other_alias > 0"
        self.assertEqual(expand_mysql_having_alias(query), query)

    def test_aggregate_only_query_drops_irrelevant_default_order(self):
        query = (
            'SELECT MAX("uid") "uid" FROM "tabCommunication" '
            'WHERE "email_account"=%(param1)s AND "uid">0 ORDER BY "creation" DESC'
        )
        expected = (
            'SELECT MAX("uid") "uid" FROM "tabCommunication" WHERE "email_account"=%(param1)s AND "uid">0'
        )
        self.assertEqual(remove_order_by_from_aggregate_only_query(query), expected)

    def test_grouped_or_nonaggregate_ordering_is_unchanged(self):
        cases = [
            'SELECT MAX("uid") FROM "tabCommunication" GROUP BY "email_account" ORDER BY "creation" DESC',
            'SELECT "name" FROM "tabCommunication" ORDER BY "creation" DESC',
        ]
        for query in cases:
            with self.subTest(query=query):
                self.assertEqual(remove_order_by_from_aggregate_only_query(query), query)

    def test_erpnext_item_zero_date_sentinel_uses_null_semantics(self):
        cases = {
            "end_of_life='0000-00-00'": "end_of_life IS NULL",
            "\"tabItem\".\"end_of_life\" = '0000-00-00'": '"tabItem"."end_of_life" IS NULL',
            "coalesce(end_of_life, '0000-00-00')='0000-00-00'": "end_of_life IS NULL",
            (
                "and (end_of_life is null or end_of_life='0000-00-00' or end_of_life > %s)"
            ): "and (end_of_life is null or end_of_life IS NULL or end_of_life > %s)",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(normalize_erpnext_item_end_of_life_zero_date(query), expected)

    def test_unrelated_zero_date_literal_is_unchanged(self):
        query = "posting_date = '0000-00-00'"
        self.assertEqual(normalize_erpnext_item_end_of_life_zero_date(query), query)

    def test_erpnext_v15_exploded_bom_group_query_is_normalized(self):
        query = """select
            bom_item.item_code,
            bom_item.idx,
            item.item_name,
            sum(bom_item.stock_qty/coalesce(bom.quantity, 1)) * 10.0 as qty,
            item.image,
            bom.project,
            bom_item.rate,
            sum(bom_item.stock_qty/coalesce(bom.quantity, 1)) * bom_item.rate * 10.0 as amount,
            item.stock_uom,
            item.item_group,
            item.allow_alternative_item,
            item_default.default_warehouse,
            item_default.expense_account as expense_account,
            item_default.buying_cost_center as cost_center,
            bom_item.source_warehouse, bom_item.operation,
            bom_item.include_item_in_manufacturing, bom_item.description, bom_item.rate,
            bom_item.sourced_by_supplier,
            (Select idx from "tabBOM Item" where item_code = bom_item.item_code and parent = %(parent)s limit 1) as idx
        from
            "tabBOM Explosion Item" bom_item
        JOIN "tabBOM" bom ON bom_item.parent = bom.name
        JOIN "tabItem" item ON item.name = bom_item.item_code
        LEFT JOIN "tabItem Default" item_default
            ON item_default.parent = item.name and item_default.company = %(company)s
        where bom_item.docstatus < 2 and bom.name = %(bom)s
        group by item_code, stock_uom
        order by idx"""
        transformed = normalize_erpnext_v15_bom_group_query(query)
        self.assertIn("MIN(bom_item.idx) AS idx", transformed)
        self.assertIn("MAX(item.item_name) AS item_name", transformed)
        self.assertIn("MAX(bom_item.rate) * 10.0 as amount", transformed)
        self.assertIn("GROUP BY bom_item.item_code, item.stock_uom", transformed)
        self.assertIn("ORDER BY MIN(bom_item.idx)", transformed)

    def test_unrelated_grouped_query_is_not_touched_by_bom_normalizer(self):
        query = 'SELECT item_code, idx FROM "tabOther" GROUP BY item_code ORDER BY idx'
        self.assertEqual(normalize_erpnext_v15_bom_group_query(query), query)

    def test_erpnext_inventory_dimension_distinct_drops_only_implicit_modified_order(self):
        query = (
            'select distinct target_fieldname as fieldname, "source_fieldname", '
            '"reference_document" as doctype, "validate_negative_stock" '
            'from "tabInventory Dimension" order by "tabInventory Dimension"."modified" DESC'
        )
        expected = (
            'select distinct target_fieldname as fieldname, "source_fieldname", '
            '"reference_document" as doctype, "validate_negative_stock" '
            'from "tabInventory Dimension"'
        )
        self.assertEqual(remove_erpnext_inventory_dimension_default_order(query), expected)

    def test_other_distinct_ordering_is_unchanged(self):
        query = (
            'select distinct "name" from "tabInventory Dimension" '
            'order by "tabInventory Dimension"."modified" DESC'
        )
        self.assertEqual(remove_erpnext_inventory_dimension_default_order(query), query)

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

    def test_numeric_truthiness_in_case_when_from_erpnext_reserved_qty(self):
        query = (
            'SELECT SUM(so_item_qty - CASE WHEN dont_reserve_qty_on_return '
            'THEN so_item_returned_qty ELSE 0 END) FROM "reserved"'
        )
        expected = (
            'SELECT SUM(so_item_qty - CASE WHEN (dont_reserve_qty_on_return <> 0) '
            'THEN so_item_returned_qty ELSE 0 END) FROM "reserved"'
        )
        self.assertEqual(convert_numeric_truthiness(query), expected)

        mysql_if = 'SELECT IF(dont_reserve_qty_on_return, so_item_returned_qty, 0)'
        transformed = apply_all_query_transformations(mysql_if)
        self.assertEqual(
            transformed,
            'SELECT CASE WHEN (dont_reserve_qty_on_return <> 0) THEN so_item_returned_qty ELSE 0 END',
        )

    def test_numeric_truthiness_case_when_leaves_real_conditions_unchanged(self):
        cases = [
            'SELECT CASE WHEN amount > 0 THEN 1 ELSE 0 END',
            'SELECT CASE WHEN TRUE THEN 1 ELSE 0 END',
            'SELECT CASE WHEN FALSE THEN 1 ELSE 0 END',
            'SELECT CASE WHEN COALESCE(flag, 0) THEN 1 ELSE 0 END',
        ]
        for query in cases:
            with self.subTest(query=query):
                self.assertEqual(convert_numeric_truthiness(query), query)

    def test_numeric_truthiness_rewrites_qualified_legacy_field(self):
        query = (
            'SELECT * FROM "tabBOM Item" bom_item WHERE (item.is_stock_item = 1 OR bom_item.is_phantom_item)'
        )
        expected = (
            'SELECT * FROM "tabBOM Item" bom_item WHERE '
            '(item.is_stock_item = 1 OR (bom_item.is_phantom_item <> 0))'
        )
        self.assertEqual(convert_numeric_truthiness(query), expected)

    def test_numeric_truthiness_leaves_qualified_comparison_unchanged(self):
        query = 'SELECT * FROM "tabBOM Item" bom_item WHERE bom_item.is_phantom_item = 1'
        self.assertEqual(convert_numeric_truthiness(query), query)

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

    def test_erpnext_landed_cost_center_is_aggregated(self):
        query = """select sum(applicable_charges), cost_center
            from "tabLanded Cost Item"
            where docstatus = '1' and purchase_receipt_item = 'item'"""
        transformed = normalize_erpnext_landed_cost_center_aggregate(query)
        self.assertIn('sum(applicable_charges), MAX(cost_center)', transformed)

    def test_unrelated_aggregate_projection_is_unchanged(self):
        query = 'select sum(amount), cost_center from "tabGL Entry"'
        self.assertEqual(normalize_erpnext_landed_cost_center_aggregate(query), query)

    def test_erpnext_negative_invoice_voucher_type_becomes_string_literal(self):
        query = """select "Purchase Invoice" as voucher_type, name as voucher_no
            from "tabPurchase Invoice" where supplier = %s"""
        transformed = normalize_erpnext_negative_invoice_voucher_literal(query)
        self.assertIn("'Purchase Invoice' AS voucher_type", transformed)

    def test_quoted_identifier_projection_is_unchanged(self):
        query = 'select "Purchase Invoice" as label from "tabPurchase Invoice"'
        self.assertEqual(normalize_erpnext_negative_invoice_voucher_literal(query), query)

    def test_voucher_literal_without_matching_invoice_table_is_unchanged(self):
        query = 'select "Purchase Invoice" as voucher_type from "tabOther"'
        self.assertEqual(normalize_erpnext_negative_invoice_voucher_literal(query), query)

    def test_payment_request_single_match_name_is_aggregated(self):
        query = """SELECT "sq0"."payment_request" FROM (
            SELECT "reference_doctype","reference_name","outstanding_amount" "allocated_amount",
            "name" "payment_request",COUNT(*) "count" FROM "tabPayment Request"
            WHERE "docstatus"= '1'
            GROUP BY "reference_doctype","reference_name","outstanding_amount"
        ) "sq0" WHERE "sq0"."count"= '1'"""
        transformed = normalize_payment_request_single_match_grouping(query)
        self.assertIn('MIN("name") "payment_request"', transformed)

    def test_other_grouped_name_projection_is_unchanged(self):
        query = 'SELECT "name",COUNT(*) "count" FROM "tabOther" GROUP BY "status"'
        self.assertEqual(normalize_payment_request_single_match_grouping(query), query)

    def test_timestamp_like_is_cast_to_text(self):
        query = """SELECT "name" FROM "tabLeave Ledger Entry" WHERE "creation" ILIKE '2026-04-01%'"""
        expected = """SELECT "name" FROM "tabLeave Ledger Entry" WHERE CAST("creation" AS TEXT) ILIKE '2026-04-01%'"""
        self.assertEqual(cast_timestamp_pattern_matches(query), expected)

    def test_non_timestamp_like_is_unchanged(self):
        query = """SELECT "name" FROM "tabUser" WHERE "name" ILIKE 'test%'"""
        self.assertEqual(cast_timestamp_pattern_matches(query), query)

    def test_mysql_inner_join_without_condition_becomes_cross_join(self):
        query = """SELECT gl.party FROM "tabGL Entry" gl
        INNER JOIN "tabSupplier" s
        WHERE s.name = gl.party GROUP BY gl.party"""
        expected = """SELECT gl.party FROM "tabGL Entry" gl
        CROSS JOIN "tabSupplier" s
        WHERE s.name = gl.party GROUP BY gl.party"""
        self.assertEqual(convert_mysql_inner_join_without_condition(query), expected)

    def test_conditioned_inner_join_is_unchanged(self):
        query = 'SELECT * FROM "tabA" a INNER JOIN "tabB" b ON b.name=a.b WHERE a.name=%s'
        self.assertEqual(convert_mysql_inner_join_without_condition(query), query)

    def test_double_quoted_mysql_string_literal_with_spaces(self):
        query = 'SELECT * FROM "tabSingles" WHERE doctype = "HR Settings" AND field = \'x\''
        expected = 'SELECT * FROM "tabSingles" WHERE doctype = \'HR Settings\' AND field = \'x\''
        self.assertEqual(convert_mysql_double_quoted_literals(query), expected)

    def test_double_quoted_mysql_identifier_shaped_literal_after_bare_field(self):
        query = 'DELETE FROM "tabUser Invitation" WHERE name = "cjlelss3v1"'
        expected = "DELETE FROM \"tabUser Invitation\" WHERE name = 'cjlelss3v1'"
        self.assertEqual(convert_mysql_double_quoted_literals(query), expected)

    def test_double_quoted_mysql_identifier_shaped_literal_after_quoted_field_is_unchanged(self):
        query = 'SELECT * FROM "tabExample" WHERE "name" = "other_column"'
        self.assertEqual(convert_mysql_double_quoted_literals(query), query)

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

    def test_zero_timestamp_pagination_param_is_normalized(self):
        values = {"param1": "0", "param2": "keep"}
        query = 'SELECT * FROM "tabNote" WHERE "creation">%(param1)s AND "name">%(param2)s'
        normalized = database_patches._normalize_zero_timestamp_params(query, values)
        self.assertEqual(normalized["param1"], "0001-01-01 00:00:00")
        self.assertEqual(normalized["param2"], "keep")
        self.assertIsNot(normalized, values)

    def test_zero_non_timestamp_param_is_unchanged(self):
        values = {"param1": "0"}
        query = 'SELECT * FROM "tabNote" WHERE "idx">%(param1)s'
        self.assertIs(database_patches._normalize_zero_timestamp_params(query, values), values)

    def test_zero_timestamp_equality_param_is_unchanged(self):
        values = {"param1": "0"}
        query = 'SELECT * FROM "tabNote" WHERE "creation"=%(param1)s'
        self.assertIs(database_patches._normalize_zero_timestamp_params(query, values), values)

    def test_postgres_json_results_are_serialized_like_mariadb(self):
        class Column:
            def __init__(self, type_code):
                self.type_code = type_code

        result = ((["a", "b"], {"enabled": True}, [1, 2], "plain"),)
        description = [Column(114), Column(3802), Column(1009), Column(25)]
        self.assertEqual(
            database_patches._serialize_json_cells(result, description),
            (('["a", "b"]', '{"enabled": true}', [1, 2], "plain"),),
        )

    def test_non_json_results_are_unchanged(self):
        class Column:
            type_code = 1009

        result = ((["a", "b"],),)
        self.assertIs(database_patches._serialize_json_cells(result, [Column()]), result)

    def test_postgres_serialization_failure_is_classified_as_deadlock(self):
        class SerializationFailure(Exception):
            pgcode = "40001"

        self.assertTrue(PostgresDatabase.is_deadlocked(SerializationFailure()))

    def test_existing_deadlock_classification_is_preserved(self):
        class DeadlockDetected(Exception):
            pgcode = "40P01"

        self.assertTrue(PostgresDatabase.is_deadlocked(DeadlockDetected()))

    def test_unrelated_postgres_error_is_not_classified_as_deadlock(self):
        class UniqueViolation(Exception):
            pgcode = "23505"

        self.assertFalse(PostgresDatabase.is_deadlocked(UniqueViolation()))

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
            "SELECT IFNULL(name, 'N/A') FROM `tabItem`",
            "SELECT IFNULL(IF(amount > 0, amount, 0), 0) FROM `tabGL Entry`",
            "SELECT DATE_FORMAT(posting_date, '%Y-%m-%d') FROM `tabGL Entry`",
            "SELECT IF(DATE_FORMAT(posting_date, '%Y-%m-%d')='2024-01-01', IFNULL(name, 'x'), 'y') FROM `tabGL Entry` FORCE INDEX (posting_date)",
            "SELECT IF(LOCATE('x', name) > 0, 1, 0) FROM `tabItem`",
            "SELECT IF(name REGEXP '^A', 1, 0) FROM `tabItem`",
            "SELECT IF(a > -45.0, 1, 0) FROM `tabTest`",
            "SELECT IF(a > 45, 1, 0) FROM `tabTest`",
        ]
        for query in corpus:
            with self.subTest(query=query):
                legacy_order = modify_query(apply_all_query_transformations(query))
                transform_hook_order = apply_all_query_transformations(modify_query(query))
                self.assertEqual(transform_hook_order, legacy_order)

    def test_patch_application_does_not_write_to_stdout(self):
        import contextlib
        import io

        database_patches.remove_postgres_fixes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            database_patches.apply_postgres_fixes()
        self.assertEqual(output.getvalue(), "")

    def test_patch_application_is_idempotent(self):
        transform = PostgresDatabase._transform_query
        database_patches.apply_postgres_fixes()
        self.assertIs(PostgresDatabase._transform_query, transform)

    def test_patch_can_be_safely_removed_and_reapplied(self):
        database_patches.remove_postgres_fixes()
        self.assertIsNot(PostgresDatabase._transform_query, database_patches.patched_transform_query)
        self.assertIsNot(PostgresDatabase.is_deadlocked, database_patches.patched_is_deadlocked)
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
