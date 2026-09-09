import sys
import types
import unittest
from unittest.mock import Mock, patch

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
            if frappe.db.is_data_truncated(Exception()):  # noqa: F821
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
            frappe.db.savepoint("insert")  # noqa: F821
            frappe.db.rollback(save_point="insert")  # noqa: F821

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
            if frappe.db.db_type == "postgres":  # noqa: F821
                child_query.select(child.name).where(conditions).for_update().run()  # noqa: F821
            if frappe.db.db_type != "postgres":  # noqa: F821
                return grouped.for_update()  # noqa: F821

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
