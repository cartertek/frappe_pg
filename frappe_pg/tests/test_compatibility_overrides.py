import sys
import types
import unittest
from datetime import time as datetime_time
from datetime import timedelta
from unittest.mock import Mock, patch

import frappe

from frappe_pg.compat.erpnext import trends_group_by


class TestTrendsGroupByCompatibility(unittest.TestCase):
    def tearDown(self):
        trends_group_by._original_based_wise_columns_query = None
        trends_group_by._patched_based_wise_columns_query = None

    @staticmethod
    def _broken_query(based_on, trans):
        if based_on == "Item":
            return {
                "based_on_select": "t2.item_code, t2.item_name, t4.default_currency as currency,",
                "based_on_group_by": "t2.item_code",
            }
        if based_on == "Customer":
            return {
                "based_on_select": "t1.customer, t1.customer_name, t1.territory, t4.default_currency as currency,",
                "based_on_group_by": "t1.customer",
            }
        return {
            "based_on_select": "t1.supplier, t1.supplier_name, t4.default_currency as currency,",
            "based_on_group_by": "t1.supplier",
        }

    @staticmethod
    def _compatible_query(based_on, trans):
        result = TestTrendsGroupByCompatibility._broken_query(based_on, trans)
        return trends_group_by._normalize_group_by(result, based_on, trans)

    def test_applies_once_when_upstream_still_needs_override(self):
        trends = types.SimpleNamespace(based_wise_columns_query=self._broken_query)
        with patch.object(trends_group_by, "_load_trends_module", return_value=trends):
            self.assertTrue(trends_group_by.is_needed())
            self.assertTrue(trends_group_by.apply())
            installed = trends.based_wise_columns_query
            self.assertFalse(trends_group_by.apply())
            self.assertIs(trends.based_wise_columns_query, installed)
            result = installed("Item", "Sales Order")
            self.assertEqual(
                result["based_on_group_by"],
                "t2.item_code, t2.item_name, t4.default_currency",
            )

    def test_does_not_wrap_already_compatible_upstream(self):
        trends = types.SimpleNamespace(based_wise_columns_query=self._compatible_query)
        original = trends.based_wise_columns_query
        with patch.object(trends_group_by, "_load_trends_module", return_value=trends):
            self.assertFalse(trends_group_by.is_needed())
            self.assertFalse(trends_group_by.apply())
            self.assertIs(trends.based_wise_columns_query, original)

    def test_remove_restores_original(self):
        trends = types.SimpleNamespace(based_wise_columns_query=self._broken_query)
        original = trends.based_wise_columns_query
        with patch.object(trends_group_by, "_load_trends_module", return_value=trends):
            self.assertTrue(trends_group_by.apply())
            self.assertTrue(trends_group_by.remove())
            self.assertIs(trends.based_wise_columns_query, original)
            self.assertFalse(trends_group_by.remove())


if __name__ == "__main__":
    unittest.main()


class TestGoalAggregationCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import goal_aggregation

        goal_aggregation._original_get_monthly_results = None
        goal_aggregation._patched_get_monthly_results = None

    def test_detects_pre_fix_function_shape(self):
        from frappe_pg.compat.frappe import goal_aggregation

        def broken(goal_doctype, goal_field, date_col, filters, aggregation="sum"):
            return Function(aggregation, goal_field)  # noqa: F821

        def fixed(goal_doctype, goal_field, date_col, filters, aggregation="sum"):
            return Function(aggregation, Table[goal_field])  # noqa: F821

        self.assertTrue(goal_aggregation._uses_string_literal_goal_field(broken))
        self.assertFalse(goal_aggregation._uses_string_literal_goal_field(fixed))

    def test_applies_once_and_restores_original(self):
        from frappe_pg.compat.frappe import goal_aggregation

        def broken(goal_doctype, goal_field, date_col, filters, aggregation="sum"):
            return Function(aggregation, goal_field)  # noqa: F821

        goal = types.SimpleNamespace(get_monthly_results=broken)
        with patch.object(goal_aggregation, "_load_goal_module", return_value=goal):
            self.assertTrue(goal_aggregation.is_needed())
            self.assertTrue(goal_aggregation.apply())
            installed = goal.get_monthly_results
            self.assertFalse(goal_aggregation.apply())
            self.assertIs(goal.get_monthly_results, installed)
            self.assertTrue(goal_aggregation.remove())
            self.assertIs(goal.get_monthly_results, broken)

    def test_fixed_upstream_is_left_untouched(self):
        from frappe_pg.compat.frappe import goal_aggregation

        def fixed(goal_doctype, goal_field, date_col, filters, aggregation="sum"):
            return Function(aggregation, Table[goal_field])  # noqa: F821

        goal = types.SimpleNamespace(get_monthly_results=fixed)
        with patch.object(goal_aggregation, "_load_goal_module", return_value=goal):
            self.assertFalse(goal_aggregation.is_needed())
            self.assertFalse(goal_aggregation.apply())
            self.assertIs(goal.get_monthly_results, fixed)


class TestSchemaTypeConversionCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import schema_type_conversion

        schema_type_conversion._original_alter = None
        schema_type_conversion._patched_alter = None

    def test_detects_native_incompatible_values_handling(self):
        from frappe_pg.compat.frappe import schema_type_conversion

        def old_alter(self):
            return self.query()

        def new_alter(self):
            if frappe.db.is_data_truncated(Exception()):
                return "Incompatible Values"

        self.assertFalse(schema_type_conversion._upstream_handles_incompatible_values(old_alter))
        self.assertTrue(schema_type_conversion._upstream_handles_incompatible_values(new_alter))

    def test_applies_once_maps_cast_failure_and_restores(self):
        from frappe_pg.compat.frappe import schema_type_conversion

        class CastFailure(Exception):
            pgcode = "22P02"

        class ValidationError(Exception):
            pass

        def old_alter(self):
            raise CastFailure()

        def throw(message, title=None):
            raise ValidationError(message)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.ValidationError = ValidationError
        fake_frappe.throw = throw
        fake_frappe._ = lambda value: value
        table = types.SimpleNamespace(alter=old_alter)
        schema = types.SimpleNamespace(doctype="Example")
        with (
            patch.object(schema_type_conversion, "_load_postgres_table", return_value=table),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(schema_type_conversion.is_needed())
            self.assertTrue(schema_type_conversion.apply())
            installed = table.alter
            self.assertFalse(schema_type_conversion.apply())
            self.assertIs(table.alter, installed)
            with self.assertRaises(ValidationError):
                installed(schema)
            self.assertTrue(schema_type_conversion.remove())
            self.assertIs(table.alter, old_alter)

    def test_unrelated_schema_error_is_preserved(self):
        from frappe_pg.compat.frappe import schema_type_conversion

        class OtherFailure(Exception):
            pgcode = "99999"

        def old_alter(self):
            raise OtherFailure()

        table = types.SimpleNamespace(alter=old_alter)
        with patch.object(schema_type_conversion, "_load_postgres_table", return_value=table):
            self.assertTrue(schema_type_conversion.apply())
            with self.assertRaises(OtherFailure):
                table.alter(types.SimpleNamespace(doctype="Example"))


class TestUniqueInsertTransactionCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import unique_insert_transaction

        unique_insert_transaction._original_db_insert = None
        unique_insert_transaction._patched_db_insert = None

    def test_detects_native_savepoint_isolation(self):
        from frappe_pg.compat.frappe import unique_insert_transaction

        def old_insert(self):
            return self.insert()

        def isolated_insert(self):
            frappe.db.savepoint("insert")
            frappe.db.rollback(save_point="insert")

        self.assertFalse(unique_insert_transaction._upstream_isolates_insert_failures(old_insert))
        self.assertTrue(unique_insert_transaction._upstream_isolates_insert_failures(isolated_insert))

    def test_unique_postgres_insert_uses_savepoint_and_restores(self):
        from frappe_pg.compat.frappe import unique_insert_transaction

        class UniqueValidationError(Exception):
            pass

        failure = UniqueValidationError("duplicate")

        def old_insert(self, *args, **kwargs):
            raise failure

        document = types.SimpleNamespace(db_insert=old_insert)
        field = types.SimpleNamespace(unique=True)
        doc = types.SimpleNamespace(meta=types.SimpleNamespace(fields=[field], autoname=None))
        fake_db = Mock(db_type="postgres")
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.db = fake_db
        with (
            patch.object(unique_insert_transaction, "_load_base_document", return_value=document),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(unique_insert_transaction.apply())
            installed = document.db_insert
            self.assertFalse(unique_insert_transaction.apply())
            with self.assertRaises(UniqueValidationError):
                installed(doc)
            fake_db.savepoint.assert_called_once()
            fake_db.rollback.assert_called_once()
            fake_db.release_savepoint.assert_not_called()
            self.assertTrue(unique_insert_transaction.remove())
            self.assertIs(document.db_insert, old_insert)

    def test_field_autoname_unique_collision_maps_to_duplicate_entry(self):
        from frappe_pg.compat.frappe import unique_insert_transaction

        class UniqueValidationError(Exception):
            pass

        class DuplicateEntryError(Exception):
            pass

        def old_insert(self, *args, **kwargs):
            raise UniqueValidationError("duplicate event")

        document = types.SimpleNamespace(db_insert=old_insert)
        event_field = types.SimpleNamespace(unique=True)
        meta = types.SimpleNamespace(
            autoname="field:event",
            fields=[event_field],
            get_field=lambda name: event_field if name == "event" else None,
        )
        doc = types.SimpleNamespace(
            doctype="HR Telemetry Milestone",
            name="_test_claim",
            meta=meta,
            get=lambda name: "_test_claim" if name == "event" else None,
        )
        fake_db = Mock(db_type="postgres")
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.db = fake_db
        fake_frappe.UniqueValidationError = UniqueValidationError
        fake_frappe.DuplicateEntryError = DuplicateEntryError

        with (
            patch.object(unique_insert_transaction, "_load_base_document", return_value=document),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(unique_insert_transaction.apply())
            with self.assertRaises(DuplicateEntryError):
                document.db_insert(doc)

        fake_db.rollback.assert_called_once()

    def test_non_autoname_unique_collision_keeps_unique_validation_error(self):
        from frappe_pg.compat.frappe import unique_insert_transaction

        class UniqueValidationError(Exception):
            pass

        class DuplicateEntryError(Exception):
            pass

        def old_insert(self, *args, **kwargs):
            raise UniqueValidationError("duplicate ordinary field")

        document = types.SimpleNamespace(db_insert=old_insert)
        field = types.SimpleNamespace(unique=True)
        doc = types.SimpleNamespace(
            doctype="Example",
            name="EX-1",
            meta=types.SimpleNamespace(autoname=None, fields=[field]),
        )
        fake_db = Mock(db_type="postgres")
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.db = fake_db
        fake_frappe.UniqueValidationError = UniqueValidationError
        fake_frappe.DuplicateEntryError = DuplicateEntryError

        with (
            patch.object(unique_insert_transaction, "_load_base_document", return_value=document),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(unique_insert_transaction.apply())
            with self.assertRaises(UniqueValidationError):
                document.db_insert(doc)

    def test_hash_autoname_retry_rolls_back_before_recursive_insert(self):
        from frappe_pg.compat.frappe import unique_insert_transaction

        attempts = []

        def old_insert(self, *args, **kwargs):
            attempts.append(self.name)
            if len(attempts) == 1:
                # Mirror Frappe's hash-collision branch: the database statement
                # failed, Frappe clears the name and recursively retries.
                self.name = None
                self.db_insert()
            return "ok"

        document = types.SimpleNamespace(db_insert=old_insert)
        fake_db = Mock(db_type="postgres")
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.db = fake_db
        doc = types.SimpleNamespace(
            name="collision",
            meta=types.SimpleNamespace(autoname="hash", fields=[]),
        )

        with (
            patch.object(unique_insert_transaction, "_load_base_document", return_value=document),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(unique_insert_transaction.apply())
            doc.db_insert = lambda *args, **kwargs: document.db_insert(doc, *args, **kwargs)
            self.assertEqual(document.db_insert(doc), "ok")

        self.assertEqual(len(attempts), 2)
        # The recursive retry restores the outer savepoint before creating its own.
        fake_db.rollback.assert_called_once()
        self.assertEqual(fake_db.savepoint.call_count, 2)
        self.assertEqual(fake_db.release_savepoint.call_count, 2)

    def test_ordinary_or_non_postgres_insert_has_no_savepoint(self):
        from frappe_pg.compat.frappe import unique_insert_transaction

        def old_insert(self, *args, **kwargs):
            return "ok"

        for db_type, unique in (("postgres", False), ("mariadb", True)):
            with self.subTest(db_type=db_type, unique=unique):
                document = types.SimpleNamespace(db_insert=old_insert)
                doc = types.SimpleNamespace(
                    meta=types.SimpleNamespace(fields=[types.SimpleNamespace(unique=unique)], autoname=None)
                )
                fake_db = Mock(db_type=db_type)
                fake_frappe = types.ModuleType("frappe")
                fake_frappe.db = fake_db
                unique_insert_transaction._original_db_insert = None
                unique_insert_transaction._patched_db_insert = None
                with (
                    patch.object(unique_insert_transaction, "_load_base_document", return_value=document),
                    patch.dict(sys.modules, {"frappe": fake_frappe}),
                ):
                    self.assertTrue(unique_insert_transaction.apply())
                    self.assertEqual(document.db_insert(doc), "ok")
                    fake_db.savepoint.assert_not_called()


class TestPostgresDecimalMetadataCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import postgres_decimal_metadata

        postgres_decimal_metadata._original_get_column_type = None
        postgres_decimal_metadata._patched_get_column_type = None

    def test_detects_native_precision_reporting(self):
        from frappe_pg.compat.frappe import postgres_decimal_metadata

        def old_get_column_type(self, doctype, column):
            return self.sql("select data_type")

        def fixed_get_column_type(self, doctype, column):
            return self.sql("select numeric_precision, numeric_scale")

        self.assertFalse(postgres_decimal_metadata._upstream_reports_numeric_precision(old_get_column_type))
        self.assertTrue(postgres_decimal_metadata._upstream_reports_numeric_precision(fixed_get_column_type))

    def test_enriches_numeric_column_type_and_restores_original(self):
        from frappe_pg.compat.frappe import postgres_decimal_metadata

        def old_get_column_type(self, doctype, column):
            return "numeric" if column == "amount" else "character varying"

        database = types.SimpleNamespace(get_column_type=old_get_column_type)
        instance = types.SimpleNamespace(
            sql=Mock(return_value=[{"numeric_precision": 30, "numeric_scale": 3}])
        )
        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.get_table_name = lambda doctype: f"tab{doctype}"
        with (
            patch.object(postgres_decimal_metadata, "_load_postgres_database", return_value=database),
            patch.dict(sys.modules, {"frappe.utils": fake_utils}),
        ):
            self.assertTrue(postgres_decimal_metadata.is_needed())
            self.assertTrue(postgres_decimal_metadata.apply())
            installed = database.get_column_type
            self.assertFalse(postgres_decimal_metadata.apply())
            self.assertEqual(installed(instance, "Test Decimal Config", "amount"), "decimal(30,3)")
            self.assertEqual(installed(instance, "Test Decimal Config", "title"), "character varying")
            instance.sql.assert_called_once_with(
                unittest.mock.ANY,
                ("tabTest Decimal Config", "amount"),
                as_dict=True,
            )
            self.assertTrue(postgres_decimal_metadata.remove())
            self.assertIs(database.get_column_type, old_get_column_type)

    def test_fixed_upstream_is_left_untouched(self):
        from frappe_pg.compat.frappe import postgres_decimal_metadata

        def fixed_get_column_type(self, doctype, column):
            return self.sql("select numeric_precision, numeric_scale")

        database = types.SimpleNamespace(get_column_type=fixed_get_column_type)
        with patch.object(postgres_decimal_metadata, "_load_postgres_database", return_value=database):
            self.assertFalse(postgres_decimal_metadata.is_needed())
            self.assertFalse(postgres_decimal_metadata.apply())
            self.assertIs(database.get_column_type, fixed_get_column_type)


class TestPostgresUniqueViolationCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import postgres_unique_violation

        postgres_unique_violation._original_is_unique_key_violation = None
        postgres_unique_violation._patched_is_unique_key_violation = None

    def test_applies_once_classifies_custom_unique_index_and_restores(self):
        from frappe_pg.compat.frappe import postgres_unique_violation

        class Database:
            @staticmethod
            def is_unique_key_violation(exc):
                return getattr(exc, "pgcode", None) == "23505" and "_key" in str(exc)

            @staticmethod
            def is_duplicate_entry(exc):
                return getattr(exc, "pgcode", None) == "23505"

            @staticmethod
            def is_primary_key_violation(exc):
                return "_pkey" in str(exc)

        original = Database.is_unique_key_violation
        custom_unique = type("UniqueViolation", (Exception,), {"pgcode": "23505"})("unique_bill_no")
        primary_key = type("UniqueViolation", (Exception,), {"pgcode": "23505"})("tabDoc_pkey")
        with patch.object(postgres_unique_violation, "_load_postgres_database", return_value=Database):
            self.assertTrue(postgres_unique_violation.is_needed())
            self.assertTrue(postgres_unique_violation.apply())
            self.assertTrue(postgres_unique_violation.is_applied())
            self.assertFalse(postgres_unique_violation.apply())
            self.assertTrue(Database.is_unique_key_violation(custom_unique))
            self.assertFalse(Database.is_unique_key_violation(primary_key))
            self.assertTrue(postgres_unique_violation.remove())
            self.assertIs(Database.is_unique_key_violation, original)

    def test_fixed_upstream_is_left_untouched(self):
        from frappe_pg.compat.frappe import postgres_unique_violation

        class Database:
            @staticmethod
            def is_unique_key_violation(exc):
                return Database.is_duplicate_entry(exc) and not Database.is_primary_key_violation(exc)

            @staticmethod
            def is_duplicate_entry(exc):
                return True

            @staticmethod
            def is_primary_key_violation(exc):
                return False

        original = Database.is_unique_key_violation
        with patch.object(postgres_unique_violation, "_load_postgres_database", return_value=Database):
            self.assertFalse(postgres_unique_violation.is_needed())
            self.assertFalse(postgres_unique_violation.apply())
            self.assertIs(Database.is_unique_key_violation, original)


class TestBatchValuationLockCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import batch_valuation_lock

        batch_valuation_lock._original_get_batch_stock_before_date = None
        batch_valuation_lock._patched_get_batch_stock_before_date = None

    def test_detects_postgres_safe_upstream_shape(self):
        from frappe_pg.compat.erpnext import batch_valuation_lock

        def old_method(self):
            query = child.stock_value_difference  # noqa: F821
            query = query.for_update().groupby(child.batch_no)  # noqa: F821
            return child.type_of_transaction.isin(["Inward", "Outward"])  # noqa: F821

        def fixed_method(self):
            query = grouped.where(conditions).groupby(child.batch_no)  # noqa: F821
            if frappe.db.db_type != "postgres":
                query = query.for_update()
            return query

        self.assertTrue(batch_valuation_lock._legacy_shape_supported(old_method))
        self.assertFalse(batch_valuation_lock._upstream_is_compatible(old_method))
        self.assertTrue(batch_valuation_lock._upstream_is_compatible(fixed_method))

    def test_applies_once_and_restores_original(self):
        from frappe_pg.compat.erpnext import batch_valuation_lock

        def old_method(self):
            query = child.stock_value_difference  # noqa: F821
            query = query.for_update().groupby(child.batch_no)  # noqa: F821
            return child.type_of_transaction.isin(["Inward", "Outward"])  # noqa: F821

        cls = types.SimpleNamespace(get_batch_stock_before_date=old_method)
        module = types.SimpleNamespace(BatchNoValuation=cls)
        with patch.object(batch_valuation_lock, "_load_serial_batch_bundle", return_value=module):
            self.assertTrue(batch_valuation_lock.is_needed())
            self.assertTrue(batch_valuation_lock.apply())
            self.assertIs(
                cls.get_batch_stock_before_date,
                batch_valuation_lock._compatible_get_batch_stock_before_date,
            )
            self.assertFalse(batch_valuation_lock.apply())
            self.assertTrue(batch_valuation_lock.remove())
            self.assertIs(cls.get_batch_stock_before_date, old_method)


class TestPaymentLedgerGroupingCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import payment_ledger_grouping

        payment_ledger_grouping._original_query_for_outstanding = None
        payment_ledger_grouping._patched_query_for_outstanding = None

    def test_detects_upstream_grouping_fix(self):
        from frappe_pg.compat.erpnext import payment_ledger_grouping

        def old_query(self):
            return self.ple.voucher_type

        def fixed_query(self):
            grouped = self.ple.groupby(ple.account, ple.voucher_type)  # noqa: F821
            representative = Min(ple.name).as_("representative")  # noqa: F821
            representative_ple = self.qb.DocType("Payment Ledger Entry").as_("representative_ple")
            return grouped, representative, representative_ple

        self.assertFalse(payment_ledger_grouping._upstream_is_compatible(old_query))
        self.assertTrue(payment_ledger_grouping._upstream_is_compatible(fixed_query))

    def test_applies_once_and_restores_original(self):
        from frappe_pg.compat.erpnext import payment_ledger_grouping

        def old_query(self):
            return self.ple.voucher_type

        cls = types.SimpleNamespace(query_for_outstanding=old_query)
        module = types.SimpleNamespace(QueryPaymentLedger=cls)
        with patch.object(payment_ledger_grouping, "_load_accounts_utils", return_value=module):
            self.assertTrue(payment_ledger_grouping.is_needed())
            self.assertTrue(payment_ledger_grouping.apply())
            self.assertIs(
                cls.query_for_outstanding, payment_ledger_grouping._compatible_query_for_outstanding
            )
            self.assertFalse(payment_ledger_grouping.apply())
            self.assertTrue(payment_ledger_grouping.remove())
            self.assertIs(cls.query_for_outstanding, old_query)


class TestPostgresBooleanValuesCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import postgres_boolean_values

        postgres_boolean_values._original_value_get_sql = None
        postgres_boolean_values._patched_value_get_sql = None
        postgres_boolean_values._original_modify_values = None
        postgres_boolean_values._patched_modify_values = None

    def test_applies_to_literal_and_bound_boolean_paths(self):
        from frappe_pg.compat.frappe import postgres_boolean_values

        class ValueWrapper:
            def __init__(self, value):
                self.value = value

            def get_sql(self):
                return str(self.value)

        def modify_values(values):
            def old(value):
                if isinstance(value, int):
                    return str(value)
                return value

            if isinstance(values, dict):
                return {key: old(value) for key, value in values.items()}
            return old(values)

        original_get_sql = ValueWrapper.get_sql
        original_modify_values = modify_values
        terms = types.SimpleNamespace(ParameterizedValueWrapper=ValueWrapper)
        postgres = types.SimpleNamespace(modify_values=modify_values)
        with patch.object(postgres_boolean_values, "_load_targets", return_value=(terms, postgres)):
            self.assertTrue(postgres_boolean_values.is_needed())
            self.assertTrue(postgres_boolean_values.apply())
            self.assertEqual(ValueWrapper(True).get_sql(), "1")
            self.assertEqual(
                postgres.modify_values({"enabled": True, "disabled": False}),
                {"enabled": "1", "disabled": "0"},
            )
            self.assertFalse(postgres_boolean_values.apply())
            self.assertTrue(postgres_boolean_values.remove())
            self.assertIs(ValueWrapper.get_sql, original_get_sql)
            self.assertIs(postgres.modify_values, original_modify_values)

    def test_fixed_upstream_is_left_untouched(self):
        from frappe_pg.compat.frappe import postgres_boolean_values

        class ValueWrapper:
            def get_sql(self):
                if isinstance(self.value, bool):
                    self.value = str(int(self.value))
                return str(self.value)

        def modify_values(values):
            def modify_value(value):
                if isinstance(value, bool):
                    return str(int(value))
                return value

            return modify_value(values)

        terms = types.SimpleNamespace(ParameterizedValueWrapper=ValueWrapper)
        postgres = types.SimpleNamespace(modify_values=modify_values)
        with patch.object(postgres_boolean_values, "_load_targets", return_value=(terms, postgres)):
            self.assertFalse(postgres_boolean_values.is_needed())
            self.assertFalse(postgres_boolean_values.apply())


class TestPostgresDateFunctionsCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import postgres_date_functions

        postgres_date_functions._original_initializers.clear()
        postgres_date_functions._patched_initializers.clear()

    @staticmethod
    def _module():
        class Function:
            def __init__(self, name, *args, alias=None):
                self.name = name
                self.args = args
                self.alias = alias

        class MonthName(Function):
            def __init__(self, field, alias=None):
                super().__init__("MONTHNAME", field, alias=alias)

        class Month(Function):
            def __init__(self, field, alias=None):
                super().__init__("MONTH", field, alias=alias)

        class Quarter(Function):
            def __init__(self, field, alias=None):
                super().__init__("QUARTER", field, alias=alias)

        return types.SimpleNamespace(
            Function=Function,
            MonthName=MonthName,
            Month=Month,
            Quarter=Quarter,
        )

    def test_postgres_uses_database_aware_functions_and_remove_restores(self):
        from frappe_pg.compat.frappe import postgres_date_functions

        module = self._module()
        originals = {name: getattr(module, name).__init__ for name in ("MonthName", "Month", "Quarter")}
        with (
            patch.object(postgres_date_functions, "_load_custom_module", return_value=module),
            patch.object(postgres_date_functions, "_is_postgres", return_value=True),
        ):
            self.assertTrue(postgres_date_functions.is_needed())
            self.assertTrue(postgres_date_functions.apply())
            self.assertEqual(module.MonthName("date").name, "to_char")
            self.assertEqual(module.MonthName("date").args, ("date", "FMMonth"))
            self.assertEqual(module.Month("date").name, "date_part")
            self.assertEqual(module.Month("date").args, ("month", "date"))
            self.assertEqual(module.Quarter("date").args, ("quarter", "date"))
            self.assertFalse(postgres_date_functions.apply())
            self.assertTrue(postgres_date_functions.remove())
            for name, original in originals.items():
                self.assertIs(getattr(module, name).__init__, original)

    def test_mariadb_still_uses_native_functions(self):
        from frappe_pg.compat.frappe import postgres_date_functions

        module = self._module()
        with (
            patch.object(postgres_date_functions, "_load_custom_module", return_value=module),
            patch.object(postgres_date_functions, "_is_postgres", return_value=False),
        ):
            self.assertTrue(postgres_date_functions.apply())
            self.assertEqual(module.MonthName("date").name, "MONTHNAME")
            self.assertEqual(module.Month("date").name, "MONTH")
            self.assertEqual(module.Quarter("date").name, "QUARTER")
            self.assertTrue(postgres_date_functions.remove())

    def test_fixed_upstream_is_left_untouched(self):
        from frappe_pg.compat.frappe import postgres_date_functions

        module = self._module()

        def fixed_init(self, field, alias=None):
            if _is_postgres():  # noqa: F821
                return self.to_char(field) or self.date_part(field)
            return None

        for name in ("MonthName", "Month", "Quarter"):
            getattr(module, name).__init__ = fixed_init

        with patch.object(postgres_date_functions, "_load_custom_module", return_value=module):
            self.assertFalse(postgres_date_functions.is_needed())
            self.assertFalse(postgres_date_functions.apply())


class TestStockAgeingPostgresCursorCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import stock_ageing_postgres_cursor

        stock_ageing_postgres_cursor._original = None
        stock_ageing_postgres_cursor._patched = None

    def test_postgres_buffers_sle_before_processing_and_restores_original(self):
        from frappe_pg.compat.erpnext import stock_ageing_postgres_cursor

        events = []

        class FIFOSlots:
            def __init__(self):
                self.sle = None
                self.filters = {"show_warehouse_wise_stock": True}
                self.item_details = {"ok": True}

            def generate(self):
                stock_ledger_entries = self._get_stock_ledger_entries()
                with frappe.db.unbuffered_cursor():
                    return list(stock_ledger_entries)

            def _get_bundle_wise_details(self, stock_ledger_entries):
                return {}, {}

            def prepare_stock_reco_voucher_wise_count(self):
                events.append("prepare")

            def _prefetch_batchwise_valuations(self):
                events.append("prefetch-batch")

            def _prefetch_valuation_methods(self):
                events.append("prefetch-method")

            def _get_stock_ledger_entries(self):
                def rows():
                    events.append("yield-1")
                    yield {"name": "A"}
                    events.append("yield-2")
                    yield {"name": "B"}

                return rows()

            def _process_stock_ledger_entry(self, row, serials, batches):
                events.append(f"process-{row['name']}")

            def _recompute_moving_average_slots(self):
                events.append("recompute")

            def _rebalance_batch_slots(self):
                events.append("rebalance")

            def _aggregate_details_by_item(self, details):
                return details

        module = types.SimpleNamespace(FIFOSlots=FIFOSlots, get_float_precision=lambda: 6)
        fake_frappe = types.SimpleNamespace(db=types.SimpleNamespace(db_type="postgres"))
        original = FIFOSlots.generate
        with (
            patch.object(stock_ageing_postgres_cursor, "_load", return_value=module),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(stock_ageing_postgres_cursor.is_needed())
            self.assertTrue(stock_ageing_postgres_cursor.apply())
            result = FIFOSlots().generate()
            self.assertEqual(result, {"ok": True})
            self.assertLess(events.index("yield-2"), events.index("process-A"))
            self.assertEqual(events.count("process-A"), 1)
            self.assertEqual(events.count("process-B"), 1)
            self.assertTrue(stock_ageing_postgres_cursor.remove())
            self.assertIs(FIFOSlots.generate, original)

    def test_postgres_explicit_sle_avoids_unbuffered_cursor(self):
        from frappe_pg.compat.erpnext import stock_ageing_postgres_cursor

        events = []

        class FIFOSlots:
            def __init__(self):
                self.sle = [{"name": "A"}, {"name": "B"}]
                self.filters = {"show_warehouse_wise_stock": True}
                self.item_details = {"ok": True}

            def generate(self):
                stock_ledger_entries = self.sle
                with frappe.db.unbuffered_cursor():
                    if stock_ledger_entries is None:
                        stock_ledger_entries = self._get_stock_ledger_entries()
                    for row in stock_ledger_entries:
                        self._process_stock_ledger_entry(row, {}, {})
                return self.item_details

            def _get_bundle_wise_details(self, rows):
                events.append(("bundles", len(rows)))
                return {}, {}

            def prepare_stock_reco_voucher_wise_count(self):
                events.append("prepare")

            def _prefetch_batchwise_valuations(self):
                raise AssertionError("explicit rows must not prefetch")

            def _prefetch_valuation_methods(self):
                raise AssertionError("explicit rows must not prefetch")

            def _get_stock_ledger_entries(self):
                raise AssertionError("explicit rows must not be replaced")

            def _process_stock_ledger_entry(self, row, serials, batches):
                events.append(f"process-{row['name']}")

            def _recompute_moving_average_slots(self):
                events.append("recompute")

            def _rebalance_batch_slots(self):
                events.append("rebalance")

            def _aggregate_details_by_item(self, details):
                return details

        module = types.SimpleNamespace(FIFOSlots=FIFOSlots, get_float_precision=lambda: 6)
        fake_frappe = types.SimpleNamespace(db=types.SimpleNamespace(db_type="postgres"))
        with (
            patch.object(stock_ageing_postgres_cursor, "_load", return_value=module),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(stock_ageing_postgres_cursor.apply())
            result = FIFOSlots().generate()
            self.assertEqual(result, {"ok": True})
            self.assertEqual(events[:3], [("bundles", 2), "prepare", "process-A"])
            self.assertIn("process-B", events)


class TestProductBundleBalanceGroupingCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import product_bundle_balance_grouping

        product_bundle_balance_grouping._original = None
        product_bundle_balance_grouping._patched = None

    def test_applies_only_to_grouped_query_that_selects_unused_name(self):
        from frappe_pg.compat.erpnext import product_bundle_balance_grouping

        def old(filters, items):
            query = sle.select(sle.item_code, sle.warehouse, sle.name, Max(sle.posting_datetime))  # noqa: F821
            return query.groupby(sle.item_code, sle.warehouse)  # noqa: F821

        module = types.SimpleNamespace(get_item_wise_max_posting_datetime=old)
        with patch.object(product_bundle_balance_grouping, "_load", return_value=module):
            self.assertTrue(product_bundle_balance_grouping.is_needed())
            self.assertTrue(product_bundle_balance_grouping.apply())
            self.assertIs(
                module.get_item_wise_max_posting_datetime, product_bundle_balance_grouping._compatible
            )
            self.assertFalse(product_bundle_balance_grouping.apply())
            self.assertTrue(product_bundle_balance_grouping.remove())
            self.assertIs(module.get_item_wise_max_posting_datetime, old)


class TestOpeningInvoiceSavepointRegistryCoverage(unittest.TestCase):
    def test_opening_invoice_savepoint_is_registered(self):
        from frappe_pg.compat import registry
        from frappe_pg.compat.erpnext import opening_invoice_savepoint

        self.assertIn(opening_invoice_savepoint, registry._COMPATIBILITY_OVERRIDES)


class TestOpeningInvoiceSavepointCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import opening_invoice_savepoint

        opening_invoice_savepoint._original = None
        opening_invoice_savepoint._patched = None

    def test_applies_to_full_rollback_implementation_and_restores(self):
        from frappe_pg.compat.erpnext import opening_invoice_savepoint

        def old_start_import(invoices):
            try:
                return invoices
            except Exception:
                frappe.db.rollback()
                doc.log_error("Opening invoice creation failed")  # noqa: F821

        module = types.SimpleNamespace(start_import=old_start_import)
        with patch.object(opening_invoice_savepoint, "_load", return_value=module):
            self.assertTrue(opening_invoice_savepoint.is_needed())
            self.assertTrue(opening_invoice_savepoint.apply())
            self.assertIs(module.start_import, opening_invoice_savepoint._compatible_start_import)
            self.assertFalse(opening_invoice_savepoint.apply())
            self.assertTrue(opening_invoice_savepoint.remove())
            self.assertIs(module.start_import, old_start_import)


class TestAccountsReceivableGLBalanceCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import accounts_receivable_gl_balance

        accounts_receivable_gl_balance._original = None
        accounts_receivable_gl_balance._patched = None

    def test_detects_legacy_grouped_as_list_shape_and_restores(self):
        from frappe_pg.compat.erpnext import accounts_receivable_gl_balance

        def old(report_date, company, account_type):
            balance_calc_fields = ["party", "SUM(debit - credit) AS balance"]
            return frappe.db.get_all("GL Entry", fields=balance_calc_fields, group_by="party", as_list=1)

        module = types.SimpleNamespace(get_gl_balance=old)
        with patch.object(accounts_receivable_gl_balance, "_load", return_value=module):
            self.assertTrue(accounts_receivable_gl_balance.is_needed())
            self.assertTrue(accounts_receivable_gl_balance.apply())
            self.assertIs(module.get_gl_balance, accounts_receivable_gl_balance._compatible)
            self.assertTrue(accounts_receivable_gl_balance.remove())
            self.assertIs(module.get_gl_balance, old)


class TestCompatibilityRegistryCoverage(unittest.TestCase):
    def test_period_closing_postgres_cursor_is_registered(self):
        from frappe_pg.compat import registry
        from frappe_pg.compat.erpnext import period_closing_postgres_cursor

        self.assertIn(period_closing_postgres_cursor, registry._COMPATIBILITY_OVERRIDES)


class TestPeriodClosingPostgresCursorCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import period_closing_postgres_cursor

        period_closing_postgres_cursor._original = None
        period_closing_postgres_cursor._patched = None

    def test_postgres_buffers_gl_rows_and_restores_original(self):
        from frappe_pg.compat.erpnext import period_closing_postgres_cursor

        events = []

        class PeriodClosingVoucher:
            def get_account_balances_based_on_dimensions(self, report_type):
                with frappe.db.unbuffered_cursor():
                    return self.get_gl_entries_for_current_period(report_type, as_iterator=True)

            def get_accounting_dimension_fields(self):
                events.append("dimensions")

            def get_gl_entries_for_current_period(
                self, report_type, only_opening_entries=False, as_iterator=False
            ):
                events.append((report_type, only_opening_entries, as_iterator))
                return [types.SimpleNamespace(value=1), types.SimpleNamespace(value=2)]

            def set_account_balance_dict(self, gle, acc):
                acc[gle.value] = True
                return acc

            def is_first_period_closing_voucher(self):
                return False

        module = types.SimpleNamespace(PeriodClosingVoucher=PeriodClosingVoucher)
        fake_frappe = types.SimpleNamespace(db=types.SimpleNamespace(db_type="postgres"), _dict=dict)
        original = PeriodClosingVoucher.get_account_balances_based_on_dimensions
        with (
            patch.object(period_closing_postgres_cursor, "_load", return_value=module),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(period_closing_postgres_cursor.is_needed())
            self.assertTrue(period_closing_postgres_cursor.apply())
            result = PeriodClosingVoucher().get_account_balances_based_on_dimensions("Profit and Loss")
            self.assertEqual(result, {1: True, 2: True})
            self.assertIn(("Profit and Loss", False, False), events)
            self.assertTrue(period_closing_postgres_cursor.remove())
            self.assertIs(PeriodClosingVoucher.get_account_balances_based_on_dimensions, original)


class TestPostgresAutomaticIndexDropCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.frappe import postgres_automatic_index_drop

        postgres_automatic_index_drop._original_alter = None
        postgres_automatic_index_drop._patched_alter = None

    def test_drop_index_sql_is_namespaced_after_alter_state_is_built(self):
        from frappe_pg.compat.frappe import postgres_automatic_index_drop as patch_module

        queries = []

        def old_alter(self):
            return frappe.db.sql('DROP INDEX IF EXISTS "middle_name" ;')

        table_class = type("FakePostgresTable", (), {"alter": old_alter})

        class FakeDB:
            def sql(self, query, *args, **kwargs):
                queries.append(query)
                return "ok"

        fake_db = FakeDB()
        with (
            patch.object(patch_module, "_load_postgres_table", return_value=table_class),
            patch.object(patch_module, "_needs_patch", return_value=True),
            patch.object(frappe, "db", fake_db),
        ):
            self.assertTrue(patch_module.apply())
            table = table_class()
            table.table_name = "tabUser"
            self.assertEqual(table.alter(), "ok")
            self.assertEqual(queries, ['DROP INDEX IF EXISTS "tabUser_middle_name_index" ;'])
            self.assertNotIn("sql", fake_db.__dict__)
            self.assertTrue(patch_module.remove())

    def test_explicit_and_already_namespaced_indexes_are_unchanged(self):
        from frappe_pg.compat.frappe import postgres_automatic_index_drop as patch_module

        query = 'DROP INDEX IF EXISTS "unique_email" ; DROP INDEX IF EXISTS "tabUser_middle_name_index" ;'
        self.assertEqual(patch_module._rewrite_automatic_drop(query, "tabUser"), query)


class TestBOMStockAnalysisGroupingCompatibility(unittest.TestCase):
    def test_producible_rows_preserve_legacy_positional_shape(self):
        from frappe_pg.compat.erpnext import bom_stock_analysis_grouping

        rows = [
            types.SimpleNamespace(
                item_code="ITEM-1",
                from_bom_no="BOM-1",
                qty_per_unit=2.0,
                available_qty=10.0,
                producible_qty=5.0,
            )
        ]
        representative = {"ITEM-1": types.SimpleNamespace(description="Part one")}

        self.assertEqual(
            bom_stock_analysis_grouping._legacy_producible_rows(rows, representative),
            [["ITEM-1", "Part one", "BOM-1", 2.0, 10.0, 5.0]],
        )


class TestFutureStockVoucherInputCompatibility(unittest.TestCase):
    def test_time_parameter_accepts_v15_string_and_timedelta(self):
        from frappe_pg.compat.erpnext import future_stock_voucher_lock

        self.assertEqual(future_stock_voucher_lock._time_parameter("00:00:00"), datetime_time(0, 0))
        self.assertEqual(
            future_stock_voucher_lock._time_parameter(timedelta(hours=12, minutes=34, seconds=56)),
            datetime_time(12, 34, 56),
        )


class TestStockBalancePostgresCursorCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import stock_balance_postgres_cursor

        stock_balance_postgres_cursor._original = None
        stock_balance_postgres_cursor._patched = None
        stock_balance_postgres_cursor._method_name = None

    def test_v15_buffers_sle_rows_on_postgres(self):
        from frappe_pg.compat.erpnext import stock_balance_postgres_cursor

        events = []

        class Query:
            def run(self, as_dict=False, as_iterator=False):
                events.append(("run", as_dict, as_iterator))
                return [types.SimpleNamespace(item_code="A")]

        class StockBalanceReport:
            def get_item_warehouse_map(self):
                with frappe.db.unbuffered_cursor():
                    self.sle_entries = self.sle_query.run(as_dict=True, as_iterator=True)

            def __init__(self):
                self.filters = {}
                self.sle_query = Query()
                self.opening_data = {}
                self.float_precision = 6
                self.inventory_dimensions = []

            def get_opening_vouchers(self):
                return []

            def prepare_stock_reco_voucher_wise_count(self):
                events.append("prepare")

            def get_group_by_key(self, entry):
                return entry.item_code

            def initialize_data(self, item_warehouse_map, key, entry):
                item_warehouse_map[key] = []

            def prepare_item_warehouse_map(self, item_warehouse_map, entry, key):
                item_warehouse_map[key].append(entry.item_code)

        module = types.SimpleNamespace(
            StockBalanceReport=StockBalanceReport,
            filter_items_with_no_transactions=lambda values, *_: values,
        )
        fake_frappe = types.SimpleNamespace(
            db=types.SimpleNamespace(db_type="postgres"),
            get_cached_doc=lambda *_: object(),
        )
        original = StockBalanceReport.get_item_warehouse_map
        with (
            patch.object(stock_balance_postgres_cursor, "_load", return_value=module),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(stock_balance_postgres_cursor.apply())
            result = StockBalanceReport().get_item_warehouse_map()
            self.assertEqual(result, {"A": ["A"]})
            self.assertIn(("run", True, False), events)
            self.assertNotIn(("run", True, True), events)
            self.assertTrue(stock_balance_postgres_cursor.remove())
            self.assertIs(StockBalanceReport.get_item_warehouse_map, original)

    def test_v16_buffers_sle_rows_on_postgres(self):
        from frappe_pg.compat.erpnext import stock_balance_postgres_cursor

        events = []

        class Query:
            def run(self, as_dict=False, as_iterator=False):
                events.append(("run", as_dict, as_iterator))
                return [types.SimpleNamespace(item_code="A")]

        class StockBalanceReport:
            def prepare_item_warehouse_map_for_current_period(self):
                with frappe.db.unbuffered_cursor():
                    self.sle_entries = self.sle_query.run(as_dict=True, as_iterator=True)

            def __init__(self):
                self.filters = {}
                self.sle_query = Query()
                self.item_warehouse_map = {}
                self.float_precision = 6
                self.inventory_dimensions = []

            def get_opening_vouchers(self):
                return []

            def prepare_stock_reco_voucher_wise_count(self):
                events.append("prepare")

            def get_group_by_key(self, entry):
                return entry.item_code

            def initialize_data(self, key, entry):
                self.item_warehouse_map[key] = []

            def prepare_item_warehouse_map(self, entry, key):
                self.item_warehouse_map[key].append(entry.item_code)

        module = types.SimpleNamespace(
            StockBalanceReport=StockBalanceReport,
            filter_items_with_no_transactions=lambda values, *_: values,
        )
        fake_frappe = types.SimpleNamespace(
            db=types.SimpleNamespace(db_type="postgres"),
            get_cached_doc=lambda *_: object(),
        )
        with (
            patch.object(stock_balance_postgres_cursor, "_load", return_value=module),
            patch.dict(sys.modules, {"frappe": fake_frappe}),
        ):
            self.assertTrue(stock_balance_postgres_cursor.apply())
            report = StockBalanceReport()
            report.prepare_item_warehouse_map_for_current_period()
            self.assertEqual(report.item_warehouse_map, {"A": ["A"]})
            self.assertIn(("run", True, False), events)
            self.assertNotIn(("run", True, True), events)
            self.assertTrue(stock_balance_postgres_cursor.remove())


class TestTotalStockSummaryGroupingCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import total_stock_summary_grouping

        total_stock_summary_grouping._original = None
        total_stock_summary_grouping._patched = None

    def test_backports_develop_grouping_only_for_legacy_shape(self):
        from frappe_pg.compat.erpnext import total_stock_summary_grouping

        def legacy(filters):
            query = query.select(item.item_code, item.description, Sum(bin.actual_qty)).groupby(  # noqa: F821
                item.item_code  # noqa: F821
            )
            return query.run()

        module = types.SimpleNamespace(get_total_stock=legacy)
        with patch.object(total_stock_summary_grouping, "_load", return_value=module):
            self.assertTrue(total_stock_summary_grouping.is_needed())
            self.assertTrue(total_stock_summary_grouping.apply())
            self.assertIs(module.get_total_stock, total_stock_summary_grouping._compatible)
            self.assertTrue(total_stock_summary_grouping.remove())
            self.assertIs(module.get_total_stock, legacy)


class TestProcessLossRegistryCoverage(unittest.TestCase):
    def test_process_loss_grouping_is_registered(self):
        from frappe_pg.compat import registry
        from frappe_pg.compat.erpnext import process_loss_grouping

        self.assertIn(process_loss_grouping, registry._COMPATIBILITY_OVERRIDES)


class TestProcessLossGroupingCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import process_loss_grouping

        process_loss_grouping._original = None
        process_loss_grouping._patched = None

    def test_detects_legacy_grouped_work_order_query_and_restores(self):
        from frappe_pg.compat.erpnext import process_loss_grouping

        def old(filters):
            query = (
                frappe.qb.from_(wo)  # noqa: F821
                .select(
                    wo.name,  # noqa: F821
                    Sum(se.total_incoming_value),  # noqa: F821
                )
                .groupby(se.work_order)  # noqa: F821
            )
            return query.run(as_dict=True)

        module = types.SimpleNamespace(get_data=old)
        with patch.object(process_loss_grouping, "_load", return_value=module):
            self.assertTrue(process_loss_grouping.is_needed())
            self.assertTrue(process_loss_grouping.apply())
            self.assertIs(module.get_data, process_loss_grouping._compatible_get_data)
            self.assertFalse(process_loss_grouping.apply())
            self.assertTrue(process_loss_grouping.remove())
            self.assertIs(module.get_data, old)


class TestAvailableSerialNoEmptySerialsCompatibility(unittest.TestCase):
    def tearDown(self):
        from frappe_pg.compat.erpnext import available_serial_no_empty_serials

        available_serial_no_empty_serials._original = None
        available_serial_no_empty_serials._patched = None

    def test_detects_legacy_null_unsafe_shape_and_restores(self):
        from frappe_pg.compat.erpnext import available_serial_no_empty_serials

        def old(available_serial_nos, sle):
            serial_nos = available_serial_nos.get(sle.serial_and_batch_bundle)
            sle.balance_serial_no = "\n".join(serial_nos)

        module = types.SimpleNamespace(update_available_serial_nos=old)
        with patch.object(available_serial_no_empty_serials, "_load", return_value=module):
            self.assertTrue(available_serial_no_empty_serials.is_needed())
            self.assertTrue(available_serial_no_empty_serials.apply())
            self.assertIs(module.update_available_serial_nos, available_serial_no_empty_serials._compatible)
            self.assertTrue(available_serial_no_empty_serials.remove())
            self.assertIs(module.update_available_serial_nos, old)

    def test_empty_serial_list_is_rendered_as_empty_string(self):
        from frappe_pg.compat.erpnext import available_serial_no_empty_serials

        module = types.SimpleNamespace(get_serial_nos=lambda value: value.split("\n") if value else [])
        sle = types.SimpleNamespace(
            serial_no=None, serial_and_batch_bundle=None, item_code="ITEM", warehouse="WH"
        )
        with patch.object(available_serial_no_empty_serials, "_load", return_value=module):
            available_serial_no_empty_serials._compatible({}, sle)
        self.assertEqual(sle.serial_no, "")
        self.assertEqual(sle.balance_serial_no, "")

    def test_registered(self):
        from frappe_pg.compat import registry
        from frappe_pg.compat.erpnext import available_serial_no_empty_serials

        self.assertIn(available_serial_no_empty_serials, registry._COMPATIBILITY_OVERRIDES)
