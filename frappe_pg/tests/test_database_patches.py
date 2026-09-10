import inspect
import types
import unittest
from datetime import time as datetime_time
from datetime import timedelta
from unittest.mock import Mock, patch

import frappe
from frappe.database.postgres.database import PostgresDatabase, modify_query
from frappe.database.utils import EmptyQueryValues

from frappe_pg.postgres import database_patches
from frappe_pg.postgres.database_patches import _normalize_time_cells
from frappe_pg.postgres.query_transformers import (
    apply_all_query_transformations,
    cast_timestamp_pattern_matches,
    convert_date_format,
    convert_erpnext_customer_suffix_unsigned,
    convert_erpnext_modified_timediff,
    convert_if_to_case,
    convert_ifnull_to_coalesce,
    convert_mysql_boolean_xor,
    convert_mysql_case_value_literals,
    convert_mysql_date_arithmetic,
    convert_mysql_datediff,
    convert_mysql_double_quoted_literals,
    convert_mysql_inner_join_without_condition,
    convert_mysql_limit_offset,
    convert_mysql_month,
    convert_mysql_monthname,
    convert_mysql_quarter,
    convert_mysql_regexp_operator,
    convert_mysql_show_index,
    convert_mysql_timestamp_pair,
    convert_mysql_update_join,
    convert_mysql_zero_date_sentinel,
    convert_numeric_truthiness,
    expand_mysql_having_alias,
    normalize_erpnext_activation_last_login_timestamp,
    normalize_erpnext_advance_payment_currency_aggregate,
    normalize_erpnext_advance_payment_reference_grouping,
    normalize_erpnext_asset_depreciation_grouping,
    normalize_erpnext_asset_empty_disposal_date,
    normalize_erpnext_bank_clearance_journal_query,
    normalize_erpnext_batch_availability_grouping,
    normalize_erpnext_batch_bundle_grouping,
    normalize_erpnext_batch_empty_expiry_date,
    normalize_erpnext_batchwise_qty_result_shape,
    normalize_erpnext_bom_items_grouping,
    normalize_erpnext_bom_stock_reports,
    normalize_erpnext_budget_requested_amount,
    normalize_erpnext_deferred_posted_literal,
    normalize_erpnext_exchange_revaluation_grouping,
    normalize_erpnext_future_journal_payment_grouping,
    normalize_erpnext_gl_account_currency_grouping,
    normalize_erpnext_gl_stock_account_value_grouping,
    normalize_erpnext_grouped_gl_financial_fields,
    normalize_erpnext_irs_1099_grouping,
    normalize_erpnext_item_end_of_life_zero_date,
    normalize_erpnext_landed_cost_center_aggregate,
    normalize_erpnext_mode_of_payment_grouping,
    normalize_erpnext_negative_invoice_voucher_literal,
    normalize_erpnext_party_specific_item_based_on,
    normalize_erpnext_production_plan_explosion_grouping,
    normalize_erpnext_production_plan_subitems_grouping,
    normalize_erpnext_purchased_items_result_shape,
    normalize_erpnext_repost_item_fields_grouping,
    normalize_erpnext_repost_item_grouping,
    normalize_erpnext_reserved_warehouse_distinct,
    normalize_erpnext_sales_order_analysis_grouping,
    normalize_erpnext_sales_pipeline_grouping,
    normalize_erpnext_serial_ledger_distinct_order,
    normalize_erpnext_stock_account_value_grouping,
    normalize_erpnext_stock_ledger_batch_grouping,
    normalize_erpnext_stock_ledger_grouped_posting_date,
    normalize_erpnext_stock_reconciliation_item_defaults,
    normalize_erpnext_stock_voucher_group_order,
    normalize_erpnext_unreconcile_payment_grouping,
    normalize_erpnext_v15_bom_group_query,
    normalize_erpnext_work_order_return_grouping,
    normalize_frappe_employee_user_casefold,
    normalize_hrms_employee_event_date_parts,
    normalize_hrms_income_tax_salary_slip_grouping,
    normalize_hrms_legacy_string_literals,
    normalize_hrms_reserved_user_alias,
    normalize_hrms_shift_assignment_empty_end_date,
    normalize_hrms_shift_attendance_grouping,
    normalize_hrms_skill_assessment_group_order,
    normalize_hrms_staffing_plan_aggregate,
    normalize_hrms_work_anniversary_date_projection,
    normalize_mysql_literal_date_time_addition,
    normalize_mysql_strpos_case_truthiness,
    normalize_payment_request_single_match_grouping,
    normalize_postgres_automatic_index_name,
    normalize_postgres_unix_timestamp_epoch,
    normalize_postgres_update_target_alias,
    qualify_frappe_grouped_order_aggregate,
    remove_distinct_unselected_default_order,
    remove_erpnext_inventory_dimension_default_order,
    remove_erpnext_item_barcode_default_order,
    remove_index_hints,
    remove_mysql_order_by_null,
    remove_order_by_from_aggregate_only_query,
)


class TestPostgresQueryValueCompatibility(unittest.TestCase):
    def test_parameterized_journal_clearance_zero_date_becomes_null_check(self):
        query = (
            'WHERE ("tabJournal Entry"."clearance_date" IS NULL OR '
            '"tabJournal Entry"."clearance_date"=%(param3)s)'
        )
        values = {"param3": "0000-00-00"}
        transformed = database_patches._normalize_erpnext_empty_date_params(query, values)
        self.assertEqual(
            transformed,
            'WHERE ("tabJournal Entry"."clearance_date" IS NULL OR "tabJournal Entry"."clearance_date" IS NULL)',
        )

    def test_parameterized_erpnext_empty_dates_become_null_checks(self):
        cases = [
            ('"disposal_date"=%(param1)s', {"param1": ""}, '"disposal_date" IS NULL'),
            ('"expiry_date"=%(param3)s', {"param3": ""}, '"expiry_date" IS NULL'),
            ('"end_of_life"=%(param2)s', {"param2": "0000-00-00"}, '"end_of_life" IS NULL'),
        ]
        for query, values, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(
                    database_patches._normalize_erpnext_empty_date_params(query, values), expected
                )

    def test_parameterized_posting_datetime_operands_are_typed(self):
        query = "and (posting_date + posting_time) > (%(posting_date)s + %(posting_time)s)"
        values = {"posting_date": "2021-01-01", "posting_time": "00:01:00"}
        self.assertEqual(
            database_patches._type_erpnext_posting_datetime_params(query, values),
            "and (posting_date + posting_time) > "
            "(CAST(%(posting_date)s AS DATE) + CAST(%(posting_time)s AS TIME))",
        )


class TestPostgresResultCompatibility(unittest.TestCase):
    def test_time_cells_match_mariadb_timedelta_contract(self):
        description = [Mock(type_code=1083), Mock(type_code=25)]
        rows = [(datetime_time(8, 30, 15, 250000), "unchanged")]
        self.assertEqual(
            _normalize_time_cells(rows, description),
            ((timedelta(hours=8, minutes=30, seconds=15, microseconds=250000), "unchanged"),),
        )

    def test_non_time_cells_are_untouched(self):
        rows = [(datetime_time(8, 30),)]
        self.assertEqual(_normalize_time_cells(rows, [Mock(type_code=25)]), rows)

    def test_postgres_time_column_metadata_matches_frappe_time_definition(self):
        columns = [
            frappe._dict(name="time", type="time without time zone"),
            frappe._dict(name="name", type="varchar(140)"),
        ]
        with patch.object(database_patches, "_original_get_table_columns_description", return_value=columns):
            result = database_patches.patched_get_table_columns_description(Mock(), "tabEvent Notifications")
        self.assertEqual(result[0].type, "time(6)")
        self.assertEqual(result[1].type, "varchar(140)")


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

    def test_postgres_automatic_index_name_is_table_scoped(self):
        query = 'CREATE INDEX IF NOT EXISTS "item_name" ON "tabItem"("item_name")'
        self.assertEqual(
            normalize_postgres_automatic_index_name(query),
            'CREATE INDEX IF NOT EXISTS "tabItem_item_name_index" ON "tabItem"("item_name")',
        )
        explicit = 'CREATE INDEX IF NOT EXISTS "custom_search" ON "tabItem"("item_name")'
        self.assertEqual(normalize_postgres_automatic_index_name(explicit), explicit)

    def test_literal_date_plus_time_is_typed(self):
        query = "WHERE posting_date + posting_time > ('2021-01-01' + '00:01:00')"
        self.assertEqual(
            normalize_mysql_literal_date_time_addition(query),
            "WHERE posting_date + posting_time > (DATE '2021-01-01' + TIME '00:01:00')",
        )

    def test_asset_empty_disposal_date_is_null(self):
        query = "(\"tabAsset\".\"disposal_date\" is NULL OR \"tabAsset\".\"disposal_date\" = '')"
        transformed = normalize_erpnext_asset_empty_disposal_date(query)
        self.assertEqual(
            transformed,
            '("tabAsset"."disposal_date" is NULL OR "tabAsset"."disposal_date" IS NULL)',
        )

    def test_strpos_case_truthiness_is_boolean(self):
        query = "CASE WHEN strpos( name, %(_txt)s) THEN strpos( name, %(_txt)s) ELSE 99999 END"
        self.assertEqual(
            normalize_mysql_strpos_case_truthiness(query),
            "CASE WHEN strpos( name, %(_txt)s) <> 0 THEN strpos( name, %(_txt)s) ELSE 99999 END",
        )

    def test_party_specific_item_legacy_based_on_uses_active_field(self):
        query = (
            'SELECT "name" FROM "tabParty Specific Item" WHERE "party"=%s '
            'AND "restrict_based_on"=\'Item\' AND "based_on"=%s'
        )
        transformed = normalize_erpnext_party_specific_item_based_on(query)
        self.assertIn('"based_on_value"=%s', transformed)
        self.assertNotIn('"based_on"=%s', transformed)

    def test_bom_stock_calculated_groups_scalar_fields(self):
        query = (
            'SELECT "tabBOM Item"."item_code","tabBOM Item"."description",'
            '"tabBOM Item"."qty_consumed_per_unit" "qty_per_unit",SUM("tabBin"."actual_qty") '
            'FROM "tabBOM Item" LEFT JOIN "tabBin" ON 1=1 GROUP BY "tabBOM Item"."item_code"'
        )
        transformed = normalize_erpnext_bom_stock_reports(query)
        self.assertIn('MAX("tabBOM Item"."description")', transformed)
        self.assertIn('SUM("tabBOM Item"."qty_consumed_per_unit")', transformed)

    def test_bom_stock_report_groups_text_scalars(self):
        query = (
            'SELECT "tabBOM Item"."item_code","tabBOM Item"."item_name","tabBOM Item"."description",'
            'SUM("tabBOM Item"."stock_qty"),"tabBOM Item"."stock_uom" '
            'FROM "tabBOM Item" GROUP BY "tabBOM Item"."item_code"'
        )
        transformed = normalize_erpnext_bom_stock_reports(query)
        self.assertIn('MAX("tabBOM Item"."item_name")', transformed)
        self.assertIn('MAX("tabBOM Item"."description")', transformed)
        self.assertIn('MAX("tabBOM Item"."stock_uom")', transformed)

    def test_supplier_quotation_purchased_items_keeps_two_columns(self):
        query = (
            'SELECT "supplier_quotation_item",SUM("qty"),MAX("modified") AS "modified" '
            'FROM "tabPurchase Order Item" WHERE "supplier_quotation"=%s '
            'GROUP BY "supplier_quotation_item" ORDER BY MAX("modified") DESC'
        )
        transformed = normalize_erpnext_purchased_items_result_shape(query)
        self.assertEqual(
            transformed,
            'SELECT "supplier_quotation_item",SUM("qty") AS "qty" FROM "tabPurchase Order Item" '
            'WHERE "supplier_quotation"=%s GROUP BY "supplier_quotation_item"',
        )

    def test_postgres_unix_timestamp_epoch_is_bigint(self):
        query = 'SELECT EXTRACT(EPOCH FROM "posting_date") FROM "tabStock Ledger Entry"'
        self.assertEqual(
            normalize_postgres_unix_timestamp_epoch(query),
            'SELECT CAST(EXTRACT(EPOCH FROM "posting_date") AS BIGINT) FROM "tabStock Ledger Entry"',
        )

    def test_postgres_unix_timestamp_epoch_handles_nested_date_idempotently(self):
        query = 'SELECT EXTRACT(EPOCH FROM DATE("creation")) FROM "tabEnergy Point Log"'
        expected = 'SELECT CAST(EXTRACT(EPOCH FROM DATE("creation")) AS BIGINT) FROM "tabEnergy Point Log"'
        transformed = normalize_postgres_unix_timestamp_epoch(query)
        self.assertEqual(transformed, expected)
        self.assertEqual(normalize_postgres_unix_timestamp_epoch(transformed), expected)

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
            "CURRENT_DATE()": "CURRENT_DATE",
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

    def test_mysql_having_alias_does_not_rewrite_same_named_column_inside_sum(self):
        query = (
            'SELECT SUM("amount_in_account_currency") AS "amount_in_account_currency" '
            'FROM "tabPayment Ledger Entry" GROUP BY "voucher_no" '
            'HAVING SUM("amount_in_account_currency") > 0'
        )
        transformed = expand_mysql_having_alias(query)
        self.assertIn('HAVING SUM("amount_in_account_currency") > 0', transformed)
        self.assertNotIn('SUM((SUM(', transformed)

    def test_exchange_revaluation_grouping_is_idempotent_for_account_currency(self):
        query = (
            'SELECT "account",MAX("party_type") AS "party_type",MAX("party") AS "party",'
            'MAX("account_currency") AS "account_currency",SUM("debit") AS "debit" '
            'FROM "tabGL Entry" GROUP BY "account",NULLIF("party_type",%(param1)s),NULLIF("party",%(param2)s)'
        )
        once = normalize_erpnext_exchange_revaluation_grouping(query)
        twice = normalize_erpnext_exchange_revaluation_grouping(once)
        self.assertEqual(once, twice)
        self.assertNotIn('AS MAX(', twice)

    def test_repost_item_grouping_is_idempotent(self):
        query = (
            'select "item_code", "warehouse", MIN("posting_date") AS "posting_date", '
            'MIN("posting_time") AS "posting_time", MIN("creation") AS "creation", '
            'MIN("posting_datetime") AS "posting_datetime" from "tabStock Ledger Entry" '
            'group by item_code, warehouse order by creation asc'
        )
        once = normalize_erpnext_repost_item_fields_grouping(query)
        twice = normalize_erpnext_repost_item_fields_grouping(once)
        self.assertEqual(once, twice)
        self.assertNotIn('AS MIN(', twice)

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

    def test_erpnext_normal_bom_items_grouping_is_qualified_and_aggregated(self):
        query = """SELECT
            bom_item.item_code, bom_item.idx, item.item_name,
            SUM(bom_item.stock_qty/COALESCE(bom.quantity, 1)) * %(qty)s AS qty,
            item.image, bom.project, item.stock_uom, item.item_group,
            item.allow_alternative_item, item_default.default_warehouse,
            item_default.expense_account AS expense_account,
            item_default.buying_cost_center AS cost_center,
            bom_item.rate, bom_item.uom, bom_item.conversion_factor,
            bom_item.source_warehouse, bom_item.operation,
            bom_item.include_item_in_manufacturing, bom_item.sourced_by_supplier,
            SUM(bom_item.stock_qty/COALESCE(bom.quantity, 1)) * bom_item.rate * %(qty)s AS amount,
            bom_item.description, bom_item.base_rate AS rate,
            bom_item.operation_row_id, bom_item.is_phantom_item, bom_item.bom_no
            FROM "tabBOM Item" bom_item
            JOIN "tabBOM" bom ON bom_item.parent = bom.name
            JOIN "tabItem" item ON item.name = bom_item.item_code
            LEFT JOIN "tabItem Default" item_default ON item_default.parent = item.name
            WHERE bom_item.docstatus < 2
            GROUP BY item_code, stock_uom, operation
            ORDER BY idx"""
        transformed = normalize_erpnext_bom_items_grouping(query)
        self.assertIn(
            "GROUP BY bom_item.item_code, item.stock_uom, bom_item.operation, "
            "bom_item.bom_no, bom_item.is_phantom_item",
            transformed,
        )
        self.assertIn("MAX(item.item_name) AS item_name", transformed)
        self.assertIn("MAX(bom_item.description) AS description", transformed)
        self.assertIn("MAX(bom_item.rate)", transformed)
        self.assertIn("ORDER BY MIN(bom_item.idx)", transformed)

    def test_erpnext_v15_bom_does_not_invent_operation_grouping(self):
        query = """SELECT bom_item.item_code, bom_item.operation, item.stock_uom,
            SUM(bom_item.stock_qty/COALESCE(bom.quantity, 1)) AS qty
            FROM "tabBOM Item" bom_item
            JOIN "tabBOM" bom ON bom_item.parent = bom.name
            JOIN "tabItem" item ON item.name = bom_item.item_code
            GROUP BY item_code, stock_uom ORDER BY idx"""
        transformed = normalize_erpnext_bom_items_grouping(query)
        self.assertIn("MAX(bom_item.operation) AS operation", transformed)
        self.assertIn("GROUP BY bom_item.item_code, item.stock_uom", transformed)
        self.assertNotIn("item.stock_uom, bom_item.operation", transformed)

    def test_erpnext_scrap_bom_grouping_qualifies_item_code(self):
        query = """SELECT bom_item.item_code, item.item_name,
            SUM(bom_item.stock_qty/COALESCE(bom.quantity, 1)) * %(qty)s AS qty,
            item.stock_uom, item.description
            FROM "tabBOM Scrap Item" bom_item
            JOIN "tabBOM" bom ON bom_item.parent = bom.name
            JOIN "tabItem" item ON item.name = bom_item.item_code
            GROUP BY item_code, stock_uom ORDER BY idx"""
        transformed = normalize_erpnext_bom_items_grouping(query)
        self.assertIn("GROUP BY bom_item.item_code, item.stock_uom", transformed)
        self.assertIn("MAX(item.item_name) AS item_name", transformed)
        self.assertIn("MAX(item.description) AS description", transformed)

    def test_unrelated_item_code_grouping_is_unchanged(self):
        query = 'SELECT item_code, SUM(qty) FROM "tabSales Order Item" GROUP BY item_code'
        self.assertEqual(normalize_erpnext_bom_items_grouping(query), query)

    def test_grouped_order_aggregate_restores_preserved_table_qualifier(self):
        query = (
            'SELECT "tabPurchase Receipt Item"."purchase_receipt_item",'
            'SUM(ABS("tabPurchase Receipt Item"."qty")) AS "qty",'
            'MAX("modified") AS "tabPurchase Receipt.modified" '
            'FROM "tabPurchase Receipt" JOIN "tabPurchase Receipt Item" ON 1=1 '
            'GROUP BY "tabPurchase Receipt Item"."purchase_receipt_item" '
            'ORDER BY "tabPurchase Receipt.modified" DESC'
        )
        transformed = qualify_frappe_grouped_order_aggregate(query)
        self.assertIn('MAX("tabPurchase Receipt"."modified") AS "tabPurchase Receipt.modified"', transformed)

    def test_unaliased_max_modified_is_unchanged(self):
        query = 'SELECT MAX("modified") FROM "tabPurchase Receipt" GROUP BY "supplier"'
        self.assertEqual(qualify_frappe_grouped_order_aggregate(query), query)

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

    def test_numeric_truthiness_still_rewrites_boolean_and(self):
        query = 'SELECT * FROM "tabX" WHERE "tabX"."a" AND "tabX"."b"'
        expected = 'SELECT * FROM "tabX" WHERE ("tabX"."a" <> 0) AND ("tabX"."b" <> 0)'
        self.assertEqual(convert_numeric_truthiness(query), expected)

    def test_erpnext_production_plan_subitems_grouping_matches_upstream_fix(self):
        query = """SELECT "tabBOM Item"."item_code","tabItem"."default_material_request_type",
            "tabItem"."item_name",SUM("tabBOM Item"."stock_qty") "qty",
            "tabItem"."is_sub_contracted_item" "is_sub_contracted",
            "tabBOM Item"."source_warehouse","tabItem"."default_bom" "default_bom",
            "tabBOM Item"."description" "description","tabBOM Item"."stock_uom" "stock_uom",
            "tabItem"."min_order_qty" "min_order_qty","tabItem"."safety_stock" "safety_stock",
            "tabItem Default"."default_warehouse","tabItem"."purchase_uom",
            "tabUOM Conversion Detail"."conversion_factor","tabBOM"."item" "main_bom_item",
            "tabBOM"."name" "main_bom","tabBOM Item"."is_phantom_item"
            FROM "tabBOM Item" JOIN "tabBOM" ON "tabBOM"."name"="tabBOM Item"."parent"
            JOIN "tabItem" ON "tabBOM Item"."item_code"="tabItem"."name"
            GROUP BY "tabBOM Item"."item_code" ORDER BY "tabBOM Item"."idx"
            """
        transformed = normalize_erpnext_production_plan_subitems_grouping(query)
        self.assertIn(
            'MAX("tabItem"."default_material_request_type") AS "default_material_request_type"', transformed
        )
        self.assertIn('MAX("tabItem"."is_sub_contracted_item") "is_sub_contracted"', transformed)
        self.assertIn('MIN("tabBOM Item"."is_phantom_item") AS "is_phantom_item"', transformed)
        self.assertIn('ORDER BY MIN("tabBOM Item"."idx")', transformed)
        self.assertIn('"tabBOM Item"."item_code"', transformed)

    def test_other_bom_item_group_query_is_unchanged(self):
        query = (
            'SELECT "tabBOM Item"."item_code",COUNT(*) FROM "tabBOM Item" GROUP BY "tabBOM Item"."item_code"'
        )
        self.assertEqual(normalize_erpnext_production_plan_subitems_grouping(query), query)

    def test_erpnext_bank_clearance_journal_grouping_matches_develop(self):
        query = (
            'SELECT "Journal Entry" "payment_document","tabJournal Entry"."name" "payment_entry",'
            '"tabJournal Entry"."cheque_no" "cheque_number","tabJournal Entry"."cheque_date",'
            'SUM("tabJournal Entry Account"."debit_in_account_currency") "debit",'
            'SUM("tabJournal Entry Account"."credit_in_account_currency") "credit",'
            '"tabJournal Entry"."posting_date","tabJournal Entry Account"."against_account",'
            '"tabJournal Entry"."clearance_date","tabJournal Entry Account"."account_currency" '
            'FROM "tabJournal Entry Account" JOIN "tabJournal Entry" '
            'ON "tabJournal Entry Account"."parent"="tabJournal Entry"."name" '
            'WHERE ("tabJournal Entry"."clearance_date" IS NULL OR '
            '"tabJournal Entry"."clearance_date"=\'0000-00-00\') '
            'GROUP BY "tabJournal Entry Account"."account","tabJournal Entry"."name" '
            'ORDER BY "tabJournal Entry"."posting_date"'
        )
        transformed = normalize_erpnext_bank_clearance_journal_query(query)
        self.assertIn('MAX("tabJournal Entry"."cheque_no") "cheque_number"', transformed)
        self.assertIn('MAX("tabJournal Entry Account"."account_currency")', transformed)
        self.assertIn('"tabJournal Entry"."clearance_date" IS NULL', transformed)
        self.assertNotIn("'0000-00-00'", transformed)
        self.assertIn('ORDER BY MAX("tabJournal Entry"."posting_date")', transformed)

        self.assertEqual(normalize_erpnext_bank_clearance_journal_query(transformed), transformed)

    def test_unrelated_journal_group_query_is_unchanged(self):
        query = 'SELECT COUNT(*) FROM "tabJournal Entry" GROUP BY "company"'
        self.assertEqual(normalize_erpnext_bank_clearance_journal_query(query), query)

    def test_erpnext_customer_suffix_unsigned_becomes_postgres_expression(self):
        query = (
            "SELECT COALESCE(MAX(CAST(SUBSTRING_INDEX(name, ' ', -1) AS UNSIGNED)), 0) "
            'FROM tabCustomer WHERE name LIKE %s'
        )
        transformed = convert_erpnext_customer_suffix_unsigned(query)
        self.assertIn('regexp_replace', transformed)
        self.assertIn('AS INTEGER)', transformed)
        self.assertNotIn('SUBSTRING_INDEX', transformed)
        self.assertNotIn('UNSIGNED', transformed)

    def test_unsigned_expression_outside_customer_is_unchanged(self):
        query = "SELECT CAST(SUBSTRING_INDEX(name, ' ', -1) AS UNSIGNED) FROM tabSupplier"
        self.assertEqual(convert_erpnext_customer_suffix_unsigned(query), query)

    def test_erpnext_advance_payment_currency_is_aggregated(self):
        query = (
            'SELECT ABS(SUM("amount")) "amount","currency" "account_currency" '
            'FROM "tabAdvance Payment Ledger Entry" WHERE "company"=%(company)s'
        )
        transformed = normalize_erpnext_advance_payment_currency_aggregate(query)
        self.assertIn('MAX("currency") "account_currency"', transformed)

    def test_other_currency_projection_is_unchanged(self):
        query = 'SELECT SUM("amount"),"currency" "account_currency" FROM "tabGL Entry"'
        self.assertEqual(normalize_erpnext_advance_payment_currency_aggregate(query), query)

    def test_erpnext_stock_voucher_group_order_matches_develop(self):
        query = (
            'SELECT "voucher_type","voucher_no","posting_date","posting_time","creation" '
            'FROM "tabStock Ledger Entry" WHERE "is_cancelled"=0 '
            'GROUP BY "voucher_type","voucher_no" '
            'ORDER BY "posting_datetime" ORDER BY "creation"'
        )
        transformed = normalize_erpnext_stock_voucher_group_order(query)
        self.assertIn('SELECT "voucher_type","voucher_no" FROM', transformed)
        self.assertIn('ORDER BY MIN("posting_datetime")', transformed)
        self.assertIn('ORDER BY MIN("posting_datetime"),MIN("creation")', transformed)
        self.assertNotIn('"posting_date","posting_time","creation" FROM', transformed)

    def test_other_stock_ledger_group_query_is_unchanged(self):
        query = 'SELECT "item_code",SUM("actual_qty") FROM "tabStock Ledger Entry" GROUP BY "item_code"'
        self.assertEqual(normalize_erpnext_stock_voucher_group_order(query), query)

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

    def test_double_quoted_mysql_identifier_shaped_literal_after_bare_field(self):
        query = 'DELETE FROM "tabUser Invitation" WHERE name = "cjlelss3v1"'
        expected = "DELETE FROM \"tabUser Invitation\" WHERE name = 'cjlelss3v1'"
        self.assertEqual(convert_mysql_double_quoted_literals(query), expected)

    def test_double_quoted_mysql_identifier_shaped_literal_after_quoted_field_is_unchanged(self):
        query = 'SELECT * FROM "tabExample" WHERE "name" = "other_column"'
        self.assertEqual(convert_mysql_double_quoted_literals(query), query)

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

    def test_mysql_regexp_operator(self):
        query = 'SELECT * FROM "tabStock Ledger Entry" WHERE CONCAT_WS(, "serial_no") REGEXP %(pattern)s'
        expected = 'SELECT * FROM "tabStock Ledger Entry" WHERE CONCAT_WS(, "serial_no") ~ %(pattern)s'
        self.assertEqual(convert_mysql_regexp_operator(query), expected)
        self.assertEqual(
            convert_mysql_regexp_operator('SELECT x FROM t WHERE x NOT REGEXP %s'),
            'SELECT x FROM t WHERE x !~ %s',
        )

    def test_mysql_timestamp_date_time_pair(self):
        query = 'SELECT TIMESTAMP("posting_date","posting_time") "posting_datetime" FROM "tabStock Entry"'
        expected = 'SELECT ("posting_date" + "posting_time") "posting_datetime" FROM "tabStock Entry"'
        self.assertEqual(convert_mysql_timestamp_pair(query), expected)
        self.assertEqual(
            convert_mysql_timestamp_pair("SELECT timestamp(sle.posting_date, sle.posting_time) FROM t sle"),
            "SELECT (sle.posting_date + sle.posting_time) FROM t sle",
        )
        self.assertEqual(
            convert_mysql_timestamp_pair('SELECT TIMESTAMP("posting_date") FROM t'),
            'SELECT TIMESTAMP("posting_date") FROM t',
        )
        self.assertEqual(
            convert_mysql_timestamp_pair("SELECT timestamp(%s, %s)"),
            "SELECT (%s + %s)",
        )
        self.assertEqual(
            convert_mysql_timestamp_pair("SELECT timestamp(%(date)s, %(time)s)"),
            "SELECT (%(date)s + %(time)s)",
        )

    def test_work_order_return_grouping_includes_original_item(self):
        query = (
            'SELECT "tabStock Entry Detail"."item_code","tabStock Entry Detail"."original_item",'
            'SUM("tabStock Entry Detail"."transfer_qty") "qty" FROM "tabStock Entry" '
            'JOIN "tabStock Entry Detail" ON "tabStock Entry Detail"."parent"="tabStock Entry"."name" '
            'WHERE "tabStock Entry"."is_return"=1 GROUP BY "tabStock Entry Detail"."item_code"'
        )
        transformed = normalize_erpnext_work_order_return_grouping(query)
        self.assertIn(
            'GROUP BY "tabStock Entry Detail"."item_code","tabStock Entry Detail"."original_item"',
            transformed,
        )
        quoted_return = query.replace('"is_return"=1', '"is_return"=\'1\'')
        transformed = normalize_erpnext_work_order_return_grouping(quoted_return)
        self.assertIn(
            'GROUP BY "tabStock Entry Detail"."item_code","tabStock Entry Detail"."original_item"',
            transformed,
        )

    def test_erpnext_irs_1099_grouping_matches_develop(self):
        query = (
            'SELECT s.supplier_group, gl.party, s.tax_id, SUM(gl.debit_in_account_currency) '
            'FROM "tabGL Entry" gl CROSS JOIN "tabSupplier" s WHERE s.name=gl.party '
            'GROUP BY gl.party ORDER BY gl.party DESC'
        )
        transformed = normalize_erpnext_irs_1099_grouping(query)
        self.assertIn('GROUP BY gl.party, s.supplier_group, s.tax_id', transformed)

    def test_asset_depreciation_grouping_includes_asset_dimensions(self):
        query = (
            'SELECT "tabAsset Depreciation Schedule"."name","tabAsset"."name",'
            '"tabAsset"."asset_category","tabAsset"."company",'
            'MIN("tabDepreciation Schedule"."idx")-1,MAX("tabDepreciation Schedule"."idx") '
            'FROM "tabAsset Depreciation Schedule" JOIN "tabAsset" ON '
            '"tabAsset Depreciation Schedule"."asset"="tabAsset"."name" '
            'JOIN "tabDepreciation Schedule" ON "tabAsset Depreciation Schedule"."name"='
            '"tabDepreciation Schedule"."parent" GROUP BY "tabAsset Depreciation Schedule"."name"'
        )
        transformed = normalize_erpnext_asset_depreciation_grouping(query)
        self.assertIn('"tabAsset"."name"', transformed.rsplit('GROUP BY', 1)[1])
        self.assertIn('"tabAsset"."asset_category"', transformed.rsplit('GROUP BY', 1)[1])
        self.assertIn('"tabAsset"."company"', transformed.rsplit('GROUP BY', 1)[1])

    def test_erpnext_repost_item_grouping_matches_develop(self):
        query = (
            'SELECT "item_code","warehouse","posting_date","posting_time","creation","posting_datetime" '
            'FROM "tabStock Ledger Entry" WHERE "voucher_no"=%s '
            'GROUP BY "item_code","warehouse" ORDER BY "creation" ASC'
        )
        transformed = normalize_erpnext_repost_item_grouping(query)
        for field in ("posting_date", "posting_time", "creation", "posting_datetime"):
            self.assertIn(f'MIN("{field}") AS "{field}"', transformed)
        self.assertIn('ORDER BY MIN("creation") ASC', transformed)

    def test_mode_of_payment_grouping_matches_develop(self):
        query = (
            'SELECT mpa.default_account, mpa.parent as mop, mp.type as type '
            'FROM "tabMode of Payment Account" mpa,"tabMode of Payment" mp '
            'WHERE mpa.parent=mp.name AND mpa.company=%s GROUP BY mp.name'
        )
        transformed = normalize_erpnext_mode_of_payment_grouping(query)
        self.assertIn('GROUP BY mpa.default_account, mpa.parent, mp.type', transformed)

    def test_bom_grouping_handles_v16_key_order(self):
        query = (
            'select bom_item.item_code, bom_item.idx, item.item_name, '
            'sum(bom_item.stock_qty/coalesce(bom.quantity, 1)) * %(qty)s as qty, '
            'item.stock_uom, bom_item.operation_row_id, bom_item.is_phantom_item, bom_item.bom_no '
            'from "tabBOM Item" bom_item '
            'JOIN "tabBOM" bom ON bom_item.parent = bom.name '
            'JOIN "tabItem" item ON item.name = bom_item.item_code '
            'where bom_item.docstatus < 2 '
            'group by item_code, operation_row_id, stock_uom order by idx'
        )
        transformed = normalize_erpnext_bom_items_grouping(query)
        self.assertIn(
            'GROUP BY bom_item.item_code, bom_item.operation_row_id, item.stock_uom, '
            'bom_item.bom_no, bom_item.is_phantom_item',
            transformed,
        )
        self.assertIn('ORDER BY MIN(bom_item.idx)', transformed)
        self.assertIn('JOIN "tabItem" item ON item.name = bom_item.item_code', transformed)
        self.assertIn('where bom_item.docstatus < 2 GROUP BY', transformed)
        self.assertNotIn('ONGROUP', transformed)

    def test_double_quoted_literal_after_legacy_qualified_field(self):
        query = 'WHERE entry.purpose = "Manufacture" AND "entry"."status" = "other"."status"'
        transformed = convert_mysql_double_quoted_literals(query)
        self.assertIn("entry.purpose = 'Manufacture'", transformed)
        self.assertIn('"entry"."status" = "other"."status"', transformed)

    def test_same_name_double_quoted_rhs_stays_identifier(self):
        query = 'UPDATE "tabPayment Schedule" SET paid_amount = "paid_amount" + %s'
        self.assertEqual(convert_mysql_double_quoted_literals(query), query)

    def test_budget_requested_amount_moves_rate_inside_sum(self):
        query = (
            'SELECT SUM(COALESCE("tabMaterial Request Item"."stock_qty",0)-'
            'COALESCE("tabMaterial Request Item"."ordered_qty",0))*'
            '"tabMaterial Request Item"."rate" "amount" '
            'FROM "tabMaterial Request" INNER JOIN "tabMaterial Request Item" '
            'ON "tabMaterial Request"."name"="tabMaterial Request Item"."parent"'
        )
        transformed = normalize_erpnext_budget_requested_amount(query)
        self.assertIn(
            'SUM((COALESCE("tabMaterial Request Item"."stock_qty",0)-'
            'COALESCE("tabMaterial Request Item"."ordered_qty",0)) * '
            'COALESCE("tabMaterial Request Item"."rate",0))',
            transformed,
        )

    def test_batch_availability_aggregates_expiry_and_order(self):
        query = (
            'SELECT "tabSerial and Batch Entry"."batch_no",'
            '"tabSerial and Batch Entry"."warehouse",'
            'SUM("tabSerial and Batch Entry"."qty") "qty",'
            '"tabBatch"."expiry_date" '
            'FROM "tabStock Ledger Entry" '
            'INNER JOIN "tabSerial and Batch Entry" ON 1=1 '
            'INNER JOIN "tabBatch" ON 1=1 '
            'GROUP BY "tabSerial and Batch Entry"."batch_no",'
            '"tabSerial and Batch Entry"."warehouse" '
            'ORDER BY "tabBatch"."expiry_date"'
        )
        transformed = normalize_erpnext_batch_availability_grouping(query)
        self.assertIn('MAX("tabBatch"."expiry_date") AS "expiry_date"', transformed)
        self.assertIn('ORDER BY MAX("tabBatch"."expiry_date")', transformed)

    def test_serial_ledger_distinct_order_includes_creation(self):
        query = (
            'SELECT DISTINCT "tabStock Ledger Entry"."posting_datetime",'
            '"tabStock Ledger Entry"."actual_qty","tabStock Ledger Entry"."serial_no",'
            '"tabStock Ledger Entry"."serial_and_batch_bundle" '
            'FROM "tabStock Ledger Entry" LEFT JOIN "tabSerial and Batch Entry" ON 1=1 '
            'WHERE "tabStock Ledger Entry"."is_cancelled"=0 '
            'ORDER BY "tabStock Ledger Entry"."posting_datetime",'
            '"tabStock Ledger Entry"."creation"'
        )
        transformed = normalize_erpnext_serial_ledger_distinct_order(query)
        select_part = transformed.split(" FROM ", 1)[0]
        self.assertIn('"tabStock Ledger Entry"."creation"', select_part)
        self.assertEqual(normalize_erpnext_serial_ledger_distinct_order(transformed), transformed)

    def test_unrelated_distinct_order_is_unchanged(self):
        query = 'SELECT DISTINCT "warehouse" FROM "tabStock Ledger Entry" ORDER BY "creation"'
        self.assertEqual(normalize_erpnext_serial_ledger_distinct_order(query), query)

    def test_stock_ledger_batch_grouping_matches_develop(self):
        query = (
            'SELECT "tabStock Ledger Entry"."warehouse","tabStock Ledger Entry"."item_code",'
            'SUM("tabStock Ledger Entry"."actual_qty") "qty","tabStock Ledger Entry"."batch_no",'
            '"tabBatch"."expiry_date" FROM "tabStock Ledger Entry" '
            'INNER JOIN "tabBatch" ON "tabStock Ledger Entry"."batch_no"="tabBatch"."name" '
            'GROUP BY "tabStock Ledger Entry"."batch_no","tabStock Ledger Entry"."warehouse" '
            'ORDER BY "tabBatch"."expiry_date"'
        )
        transformed = normalize_erpnext_stock_ledger_batch_grouping(query)
        self.assertIn('MAX("tabStock Ledger Entry"."item_code") AS "item_code"', transformed)
        self.assertIn('MAX("tabBatch"."expiry_date") AS "expiry_date"', transformed)
        self.assertIn('ORDER BY MAX("tabBatch"."expiry_date")', transformed)
        self.assertEqual(normalize_erpnext_stock_ledger_batch_grouping(transformed), transformed)

    def test_unrelated_stock_ledger_grouping_is_unchanged(self):
        query = 'SELECT SUM("actual_qty") FROM "tabStock Ledger Entry" GROUP BY "warehouse"'
        self.assertEqual(normalize_erpnext_stock_ledger_batch_grouping(query), query)

    def test_unreconcile_payment_aggregates_dependent_fields(self):
        query = (
            'SELECT "tabPayment Ledger Entry"."company",'
            '"tabPayment Ledger Entry"."account",'
            '"tabPayment Ledger Entry"."party_type",'
            '"tabPayment Ledger Entry"."party",'
            '"tabPayment Ledger Entry"."against_voucher_type" "reference_doctype",'
            '"tabPayment Ledger Entry"."against_voucher_no" "reference_name",'
            'ABS(SUM("tabPayment Ledger Entry"."amount_in_account_currency")) "allocated_amount",'
            '"tabPayment Ledger Entry"."account_currency" '
            'FROM "tabPayment Ledger Entry" '
            'GROUP BY "tabPayment Ledger Entry"."against_voucher_no"'
        )
        transformed = normalize_erpnext_unreconcile_payment_grouping(query)
        for field in (
            "company",
            "account",
            "party_type",
            "party",
            "against_voucher_type",
            "account_currency",
        ):
            self.assertIn(f'MAX("tabPayment Ledger Entry"."{field}")', transformed)
        self.assertIn('"tabPayment Ledger Entry"."against_voucher_no" "reference_name"', transformed)

    def test_reserved_warehouse_distinct_uses_grouped_earliest_creation(self):
        query = (
            'SELECT DISTINCT "tabStock Reservation Entry"."warehouse" '
            'FROM "tabStock Reservation Entry" '
            'WHERE "tabStock Reservation Entry"."docstatus"=1 '
            'ORDER BY "tabStock Reservation Entry"."creation"'
        )
        transformed = normalize_erpnext_reserved_warehouse_distinct(query)
        self.assertNotIn('SELECT DISTINCT', transformed)
        self.assertIn('GROUP BY "tabStock Reservation Entry"."warehouse"', transformed)
        self.assertIn('ORDER BY MIN("tabStock Reservation Entry"."creation")', transformed)

    def test_mysql_order_by_null_is_removed(self):
        query = 'SELECT parent FROM "tabItem Variant Attribute" GROUP BY parent ORDER BY NULL'
        self.assertEqual(
            remove_mysql_order_by_null(query),
            'SELECT parent FROM "tabItem Variant Attribute" GROUP BY parent',
        )

    def test_mysql_limit_offset_is_reordered(self):
        self.assertEqual(
            convert_mysql_limit_offset('SELECT name FROM "tabItem" LIMIT %(start)s, %(page_len)s'),
            'SELECT name FROM "tabItem" LIMIT %(page_len)s OFFSET %(start)s',
        )
        self.assertEqual(
            convert_mysql_limit_offset('SELECT name FROM t LIMIT 10, 20'),
            'SELECT name FROM t LIMIT 20 OFFSET 10',
        )

    def test_erpnext_modified_timediff_is_timestamp_subtraction(self):
        cases = {
            "select TIMEDIFF(%s, %s)": ("SELECT (CAST(%s AS timestamp) - CAST(%s AS timestamp))"),
            "select TIMEDIFF('2026-09-08 12:00:01', '2026-09-08 12:00:00')": (
                "SELECT (CAST('2026-09-08 12:00:01' AS timestamp) - CAST('2026-09-08 12:00:00' AS timestamp))"
            ),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(convert_erpnext_modified_timediff(query), expected)

    def test_timediff_transform_is_limited_to_erpnext_select_shape(self):
        query = "SELECT name, TIMEDIFF(end_time, start_time) FROM tabExample"
        self.assertEqual(convert_erpnext_modified_timediff(query), query)

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

    def test_advance_payment_reference_grouping_aggregates_dependent_fields(self):
        query = (
            'SELECT "company","against_voucher_type" "reference_doctype",'
            '"against_voucher_no" "reference_name",ABS(SUM("amount")) "allocated_amount",'
            '"currency" FROM "tabAdvance Payment Ledger Entry" '
            'GROUP BY "against_voucher_no" HAVING ABS(SUM("amount"))>0'
        )
        transformed = normalize_erpnext_advance_payment_reference_grouping(query)
        self.assertIn('MAX("company") AS "company"', transformed)
        self.assertIn('MAX("against_voucher_type") "reference_doctype"', transformed)
        self.assertIn('MAX("currency") AS "currency"', transformed)

    def test_postgres_update_target_alias_unqualifies_set_column(self):
        query = (
            'UPDATE "tabAdvance Taxes and Charges" "at" '
            'SET "at"."allocated_amount"="at"."allocated_amount"+3000.0 '
            'WHERE "at"."name"=%(param1)s'
        )
        expected = (
            'UPDATE "tabAdvance Taxes and Charges" AS "at" '
            'SET "allocated_amount"="at"."allocated_amount"+3000.0 '
            'WHERE "at"."name"=%(param1)s'
        )
        self.assertEqual(normalize_postgres_update_target_alias(query), expected)

    def test_raw_gl_account_currency_is_aggregated(self):
        query = (
            'select account, account_currency, sum(debit) as debit, sum(credit) as credit '
            'FROM "tabGL Entry" group by account'
        )
        transformed = normalize_erpnext_gl_account_currency_grouping(query)
        self.assertIn('MAX("account_currency") AS "account_currency"', transformed)

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

    def test_mysql_case_value_literal(self):
        query = 'SELECT CASE add_deduct_tax WHEN "Add" THEN tax_amount ELSE -tax_amount END FROM "tabTax"'
        self.assertIn("WHEN 'Add'", convert_mysql_case_value_literals(query))

    def test_future_journal_payment_grouping(self):
        query = (
            'SELECT "tabJournal Entry Account"."reference_name" "invoice_no",'
            '"tabJournal Entry Account"."party","tabJournal Entry Account"."party_type",'
            '"tabJournal Entry"."posting_date" "future_date","tabJournal Entry"."cheque_no" "future_ref",'
            'SUM("tabJournal Entry Account"."credit_in_account_currency") "future_amount" '
            'FROM "tabJournal Entry" JOIN "tabJournal Entry Account" '
            'ON "tabJournal Entry Account"."parent"="tabJournal Entry"."name" '
            'HAVING "future_amount">0'
        )
        transformed = normalize_erpnext_future_journal_payment_grouping(query)
        self.assertIn('GROUP BY "tabJournal Entry"."name"', transformed)
        self.assertIn('"tabJournal Entry Account"."reference_name"', transformed)

    def test_mysql_date_sub_now_interval(self):
        query = 'select name from "tabUser" where last_login > date_sub(now(), interval 2 day) limit 1'
        self.assertEqual(
            convert_mysql_date_arithmetic(query),
            "select name from \"tabUser\" where last_login > NOW() - INTERVAL '2 day' limit 1",
        )

    def test_mysql_datediff_accepts_aggregate_operand(self):
        query = 'SELECT DATEDIFF(CURRENT_DATE,MAX("tabSales Order"."transaction_date")) "days"'
        transformed = convert_mysql_datediff(query)
        self.assertNotIn('DATEDIFF', transformed.upper())
        self.assertIn('CAST(MAX("tabSales Order"."transaction_date") AS DATE)', transformed)

    def test_mysql_monthname(self):
        self.assertEqual(
            convert_mysql_monthname('SELECT MONTHNAME("posting_date") FROM "tabGL Entry"'),
            "SELECT TO_CHAR(\"posting_date\", 'FMMonth') FROM \"tabGL Entry\"",
        )

    def test_mysql_show_index_for_legacy_erpnext_perf_test(self):
        query = "SHOW INDEX FROM \"tabBin\" WHERE Column_name = 'item_code' AND Seq_in_index = '1'"
        transformed = convert_mysql_show_index(query)
        self.assertIn('FROM pg_index', transformed)
        self.assertIn("t.relname='tabBin'", transformed)
        self.assertIn("a.attname='item_code'", transformed)

    def test_mysql_boolean_xor_for_balance_aggregates(self):
        query = (
            "SELECT SUM(\"debit\")-SUM(\"credit\")= '0' XOR "
            "SUM(\"debit_in_account_currency\")-SUM(\"credit_in_account_currency\")= '0' \"zero_balance\" "
            'FROM "tabGL Entry"'
        )
        transformed = convert_mysql_boolean_xor(query)
        self.assertNotIn(' XOR ', transformed)
        self.assertIn('<>', transformed)

    def test_deferred_posted_marker_is_literal(self):
        query = (
            'SELECT "tabSales Invoice Item"."name",SUM("tabGL Entry"."debit") "debit","posted" '
            'FROM "tabSales Invoice Item" LEFT JOIN "tabGL Entry" ON 1=1 '
            'GROUP BY "tabSales Invoice Item"."name"'
        )
        transformed = normalize_erpnext_deferred_posted_literal(query)
        self.assertIn("'posted' AS \"posted\" FROM", transformed)

    def test_quoted_having_aliases_are_expanded(self):
        query = (
            'SELECT SUM("credit") "future_amount",SUM("debit") "balance" FROM "tabGL Entry" '
            'HAVING "future_amount">0 AND "balance"<>0'
        )
        transformed = expand_mysql_having_alias(query)
        self.assertIn('HAVING (SUM("credit"))>0', transformed)
        self.assertIn('(SUM("debit"))<>0', transformed)

    def test_repost_item_grouping_uses_earliest_fields(self):
        query = (
            'SELECT "item_code","warehouse","posting_date","posting_time","creation","posting_datetime" '
            "FROM \"tabStock Ledger Entry\" WHERE \"voucher_type\"='Stock Entry' "
            'GROUP BY "item_code","warehouse"'
        )
        transformed = normalize_erpnext_repost_item_fields_grouping(query)
        for field in ("posting_date", "posting_time", "creation", "posting_datetime"):
            self.assertIn(f'MIN("{field}") AS "{field}"', transformed)

    def test_batchwise_qty_drops_postgres_order_helper_column(self):
        query = (
            'SELECT "batch_no",SUM("qty") AS "qty",MAX(creation) AS "creation" '
            'FROM "tabSerial and Batch Entry" GROUP BY "batch_no" ORDER BY "creation" DESC'
        )
        transformed = normalize_erpnext_batchwise_qty_result_shape(query)
        self.assertEqual(
            transformed,
            'SELECT "batch_no",SUM("qty") AS "qty" FROM "tabSerial and Batch Entry" GROUP BY "batch_no"',
        )

    def test_batchwise_qty_rebuilds_projection_for_unknown_order_helper_alias(self):
        query = (
            'SELECT "batch_no",SUM("qty") AS "qty",MAX("creation") AS "_order_by" '
            'FROM "tabSerial and Batch Entry" GROUP BY "batch_no" ORDER BY "_order_by" DESC'
        )
        self.assertEqual(
            normalize_erpnext_batchwise_qty_result_shape(query),
            'SELECT "batch_no",SUM("qty") AS "qty" FROM "tabSerial and Batch Entry" GROUP BY "batch_no"',
        )

    def test_mysql_month_is_extracted(self):
        self.assertEqual(
            convert_mysql_month('SELECT MONTH("expected_closing")'),
            'SELECT EXTRACT(MONTH FROM "expected_closing")',
        )

    def test_activation_last_login_text_is_cast_for_timestamp_comparison(self):
        query = 'select name from "tabUser" where last_login > now() - INTERVAL \'2 day\' limit 1'
        transformed = normalize_erpnext_activation_last_login_timestamp(query)
        self.assertIn('CAST(NULLIF("last_login", \'\') AS timestamp) > now()', transformed)

    def test_batch_empty_expiry_date_is_treated_as_null(self):
        query = "(\"tabBatch\".\"expiry_date\" is NULL OR \"tabBatch\".\"expiry_date\" = '')"
        transformed = normalize_erpnext_batch_empty_expiry_date(query)
        self.assertNotIn("= ''", transformed)
        self.assertIn('"tabBatch"."expiry_date" IS NULL', transformed)

    def test_sales_pipeline_count_name_groups_display_month(self):
        query = (
            'select "_assign" as opportunity_owner, count(name) as count, '
            "TO_CHAR(expected_closing, 'FMMonth') as month, "
            'EXTRACT(MONTH FROM expected_closing) as "EXTRACT(MONTH FROM expected_closing)" '
            'from "tabOpportunity" group by _assign,EXTRACT(MONTH FROM expected_closing) '
            'order by "EXTRACT(MONTH FROM expected_closing)"'
        )
        transformed = normalize_erpnext_sales_pipeline_grouping(query)
        self.assertIn(
            "TO_CHAR(expected_closing, 'FMMonth')",
            transformed.split(' order by ')[0],
        )
        self.assertIn(
            "TO_CHAR(expected_closing, 'FMMonth')", transformed[transformed.lower().index('group by') :]
        )

    def test_distinct_implicit_modified_order_is_removed(self):
        query = 'SELECT DISTINCT "fieldname" FROM "tabDocField" ORDER BY "tabDocField"."modified" ASC'
        self.assertEqual(
            remove_distinct_unselected_default_order(query), 'SELECT DISTINCT "fieldname" FROM "tabDocField"'
        )

    def test_grouped_sle_posting_date_uses_max(self):
        query = (
            'SELECT "item_code","warehouse","batch_no","posting_date",SUM("actual_qty") "qty" '
            'FROM "tabStock Ledger Entry" GROUP BY "voucher_no","batch_no","item_code","warehouse"'
        )
        transformed = normalize_erpnext_stock_ledger_grouped_posting_date(query)
        self.assertIn('MAX("posting_date") AS "posting_date"', transformed)

    def test_having_alias_expands_inside_boolean_parentheses(self):
        query = (
            'SELECT SUM("debit")-SUM("credit") "balance",'
            'SUM("debit_in_account_currency")-SUM("credit_in_account_currency") '
            '"balance_in_account_currency" FROM "tabGL Entry" GROUP BY "account" '
            'HAVING "balance"<>"balance_in_account_currency" AND '
            '("balance_in_account_currency"<>0 OR "balance"<>0)'
        )
        transformed = expand_mysql_having_alias(query)
        having = transformed[transformed.index("HAVING") :]
        self.assertNotIn('"balance_in_account_currency"', having)
        self.assertNotIn('"balance"', having)

    def test_future_journal_payments_group_by_reference_dimensions(self):
        query = (
            'SELECT "tabJournal Entry Account"."reference_name" "invoice_no",'
            '"tabJournal Entry Account"."party","tabJournal Entry Account"."party_type",'
            '"tabJournal Entry"."posting_date" "future_date","tabJournal Entry"."cheque_no" "future_ref",'
            'SUM("tabJournal Entry Account"."credit") "future_amount" '
            'FROM "tabJournal Entry" JOIN "tabJournal Entry Account" ON '
            '"tabJournal Entry Account"."parent"="tabJournal Entry"."name" '
            'HAVING SUM("tabJournal Entry Account"."credit") > 0'
        )
        transformed = normalize_erpnext_future_journal_payment_grouping(query)
        self.assertIn('GROUP BY "tabJournal Entry"."name"', transformed)
        self.assertIn('"tabJournal Entry Account"."reference_name"', transformed)
        self.assertEqual(normalize_erpnext_future_journal_payment_grouping(transformed), transformed)

    def test_grouped_gl_financial_fields_use_upstream_aggregates(self):
        query = (
            'SELECT "account","account_currency",SUM("debit") AS "debit",SUM("credit") AS "credit",'
            '"debit_in_account_currency","credit_in_account_currency","posting_date","is_opening","fiscal_year" '
            'FROM "tabGL Entry" GROUP BY "account"'
        )
        transformed = normalize_erpnext_grouped_gl_financial_fields(query)
        self.assertIn('SUM("debit_in_account_currency") AS "debit_in_account_currency"', transformed)
        self.assertIn('SUM("credit_in_account_currency") AS "credit_in_account_currency"', transformed)
        self.assertIn('MAX("posting_date") AS "posting_date"', transformed)
        self.assertIn('MAX("is_opening") AS "is_opening"', transformed)
        self.assertIn('MAX("fiscal_year") AS "fiscal_year"', transformed)

    def test_having_alias_expansion_is_idempotent_with_qualified_column(self):
        query = (
            'SELECT "tabPayment Ledger Entry"."party",'
            'SUM("tabPayment Ledger Entry"."amount") "amount",'
            '"tabPayment Ledger Entry"."account" '
            'FROM "tabPayment Ledger Entry" '
            'GROUP BY "tabPayment Ledger Entry"."party","tabPayment Ledger Entry"."account" '
            'HAVING "amount" < 0'
        )
        once = apply_all_query_transformations(query)
        twice = apply_all_query_transformations(once)
        self.assertEqual(once, twice)
        self.assertIn('HAVING (SUM("tabPayment Ledger Entry"."amount")) < 0', twice)

    def test_sales_order_analysis_groups_both_primary_keys(self):
        query = (
            'SELECT so.transaction_date as date, soi.delivery_date as delivery_date, '
            'so.name as sales_order, so.status, so.customer, soi.item_code, SUM(sii.qty) as billed_qty '
            'FROM "tabSales Order" so, "tabSales Order Item" soi '
            'LEFT JOIN "tabSales Invoice Item" sii ON sii.so_detail=soi.name '
            'WHERE soi.parent=so.name GROUP BY soi.name ORDER BY so.transaction_date ASC'
        )
        transformed = normalize_erpnext_sales_order_analysis_grouping(query)
        self.assertIn('GROUP BY soi.name, so.name', transformed)
        self.assertEqual(normalize_erpnext_sales_order_analysis_grouping(transformed), transformed)

    def test_mysql_quarter_is_extracted(self):
        query = 'SELECT QUARTER(expected_closing) FROM "tabOpportunity"'
        self.assertEqual(
            convert_mysql_quarter(query),
            'SELECT EXTRACT(QUARTER FROM expected_closing) FROM "tabOpportunity"',
        )

    def test_production_plan_explosion_aggregates_dependent_fields(self):
        query = (
            'SELECT SUM("tabBOM Explosion Item"."stock_qty") "qty",'
            '"tabItem"."item_name","tabBOM Explosion Item"."stock_uom" '
            'FROM "tabBOM Explosion Item" JOIN "tabBOM" ON 1=1 JOIN "tabItem" ON 1=1 '
            'GROUP BY "tabBOM Explosion Item"."item_code","tabBOM Explosion Item"."stock_uom"'
        )
        transformed = normalize_erpnext_production_plan_explosion_grouping(query)
        self.assertIn('MAX("tabItem"."item_name") AS "item_name"', transformed)

    def test_batch_bundle_grouping_aggregates_sle_scalars(self):
        query = (
            'SELECT "tabStock Ledger Entry"."item_code","tabStock Ledger Entry"."warehouse",'
            '"tabSerial and Batch Entry"."batch_no","tabStock Ledger Entry"."posting_date",'
            'SUM("tabSerial and Batch Entry"."qty") "actual_qty" '
            'FROM "tabStock Ledger Entry" JOIN "tabSerial and Batch Entry" ON 1=1 '
            'GROUP BY "tabStock Ledger Entry"."voucher_no","tabSerial and Batch Entry"."batch_no",'
            '"tabSerial and Batch Entry"."warehouse"'
        )
        transformed = normalize_erpnext_batch_bundle_grouping(query)
        self.assertIn('MAX("tabStock Ledger Entry"."item_code") AS "item_code"', transformed)
        self.assertIn('MAX("tabStock Ledger Entry"."warehouse") AS "warehouse"', transformed)
        self.assertIn('MAX("tabStock Ledger Entry"."posting_date") AS "posting_date"', transformed)

    def test_gl_stock_account_value_grouping_aggregates_scalars(self):
        query = (
            'select "name", "voucher_type", "voucher_no", "posting_date", '
            'sum("debit_in_account_currency") - sum("credit_in_account_currency") as "account_value" '
            'from "tabGL Entry" group by voucher_type, voucher_no'
        )
        transformed = normalize_erpnext_gl_stock_account_value_grouping(query)
        self.assertIn('MAX("name") AS "name"', transformed)
        self.assertIn('MAX("posting_date") AS "posting_date"', transformed)

    def test_employee_user_id_lookup_is_case_insensitive(self):
        query = 'SELECT "name" FROM "tabEmployee" WHERE "user_id"=%s LIMIT 1'
        transformed = normalize_frappe_employee_user_casefold(query)
        self.assertIn('LOWER("user_id") = LOWER(%s)', transformed)

    def test_stock_account_value_grouping_splits_posting_max(self):
        query = (
            'select "name", "voucher_type", "voucher_no", sum(stock_value_difference) as stock_value, '
            'MAX(posting_date, posting_time) as "posting_date, posting_time" '
            'from "tabStock Ledger Entry" group by voucher_type, voucher_no '
            'order by posting_date ASC, posting_time ASC'
        )
        transformed = normalize_erpnext_stock_account_value_grouping(query)
        self.assertNotIn('MAX(posting_date, posting_time)', transformed)
        self.assertIn('MAX("posting_date") AS "posting_date"', transformed)
        self.assertIn('MAX("posting_time") AS "posting_time"', transformed)

    def test_item_attribute_numeric_parameter_is_stringified(self):
        query = 'select v.abbr from "tabItem Attribute Value" v where v.attribute_value=%(attribute_value)s'
        values = {"attribute_value": 1.1}
        self.assertEqual(
            database_patches._normalize_item_attribute_values(query, values)["attribute_value"], "1.1"
        )

    def test_item_barcode_distinct_drops_default_modified_order(self):
        query = 'SELECT DISTINCT "barcode" FROM "tabItem Barcode" ORDER BY "tabItem Barcode"."modified" DESC'
        self.assertEqual(
            remove_erpnext_item_barcode_default_order(query),
            'SELECT DISTINCT "barcode" FROM "tabItem Barcode"',
        )

    def test_stock_reconciliation_item_defaults_drops_obsolete_grouping(self):
        query = (
            'SELECT i.name as item_code, i.item_name, id.default_warehouse as warehouse, i.stock_uom '
            'FROM "tabItem" i, "tabItem Default" id '
            'WHERE i.name=id.parent AND id.company=%s GROUP BY i.name'
        )
        transformed = normalize_erpnext_stock_reconciliation_item_defaults(query)
        self.assertNotIn("GROUP BY", transformed)
        self.assertIn("id.default_warehouse as warehouse", transformed)

    def test_stock_voucher_order_aggregates_creation_tiebreaker(self):
        query = (
            'SELECT "voucher_type","voucher_no","posting_date","posting_time","creation" '
            'FROM "tabStock Ledger Entry" GROUP BY "voucher_type","voucher_no" '
            'ORDER BY "posting_datetime","creation"'
        )
        transformed = normalize_erpnext_stock_voucher_group_order(query)
        self.assertIn('ORDER BY MIN("posting_datetime"),MIN("creation")', transformed)

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


class TestPostgresAutomaticIndexDropPatch(unittest.TestCase):
    def test_drop_index_sql_is_namespaced_after_alter_state_is_built(self):
        from frappe_pg.compat.frappe import postgres_automatic_index_drop as patch_module

        queries = []

        def old_alter(self):
            # Model Frappe's real alter path: the bare automatic index name is
            # generated inside alter(), after column state has been rebuilt.
            return frappe.db.sql('DROP INDEX IF EXISTS "middle_name" ;')

        table_class = type("FakePostgresTable", (), {"alter": old_alter})
        with (
            unittest.mock.patch.object(patch_module, "_load_postgres_table", return_value=table_class),
            unittest.mock.patch.object(patch_module, "_needs_patch", return_value=True),
            unittest.mock.patch.object(
                frappe.db, "sql", side_effect=lambda query, *args, **kwargs: queries.append(query) or "ok"
            ),
        ):
            patch_module._original_alter = None
            patch_module._patched_alter = None
            self.assertTrue(patch_module.apply())
            table = table_class()
            table.table_name = "tabUser"
            self.assertEqual(table.alter(), "ok")
            self.assertEqual(queries, ['DROP INDEX IF EXISTS "tabUser_middle_name_index" ;'])
            # Restoring a bound method onto the instance would shadow future
            # class-level instrumentation of PostgresDatabase.sql.
            self.assertNotIn("sql", getattr(frappe.db, "__dict__", {}))
            self.assertTrue(patch_module.remove())

    def test_explicit_and_already_namespaced_indexes_are_unchanged(self):
        from frappe_pg.compat.frappe import postgres_automatic_index_drop as patch_module

        query = 'DROP INDEX IF EXISTS "unique_email" ; ' 'DROP INDEX IF EXISTS "tabUser_middle_name_index" ;'
        self.assertEqual(patch_module._rewrite_automatic_drop(query, "tabUser"), query)
