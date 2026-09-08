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

        def old_alter(self):
            raise CastFailure()

        table = types.SimpleNamespace(alter=old_alter)
        schema = types.SimpleNamespace(doctype="Example")
        with (
            patch.object(schema_type_conversion, "_load_postgres_table", return_value=table),
            patch("frappe.throw", side_effect=__import__("frappe").ValidationError("incompatible")),
        ):
            self.assertTrue(schema_type_conversion.is_needed())
            self.assertTrue(schema_type_conversion.apply())
            installed = table.alter
            self.assertFalse(schema_type_conversion.apply())
            self.assertIs(table.alter, installed)
            with self.assertRaises(__import__("frappe").ValidationError):
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
        import frappe

        from frappe_pg.compat.frappe import unique_insert_transaction

        failure = frappe.UniqueValidationError("duplicate")

        def old_insert(self, *args, **kwargs):
            raise failure

        document = types.SimpleNamespace(db_insert=old_insert)
        field = types.SimpleNamespace(unique=True)
        doc = types.SimpleNamespace(meta=types.SimpleNamespace(fields=[field]))
        fake_db = Mock(db_type="postgres")
        with (
            patch.object(unique_insert_transaction, "_load_base_document", return_value=document),
            patch.object(frappe, "db", fake_db),
        ):
            self.assertTrue(unique_insert_transaction.apply())
            installed = document.db_insert
            self.assertFalse(unique_insert_transaction.apply())
            with self.assertRaises(frappe.UniqueValidationError):
                installed(doc)
            fake_db.savepoint.assert_called_once()
            fake_db.rollback.assert_called_once()
            fake_db.release_savepoint.assert_not_called()
            self.assertTrue(unique_insert_transaction.remove())
            self.assertIs(document.db_insert, old_insert)

    def test_ordinary_or_non_postgres_insert_has_no_savepoint(self):
        import frappe

        from frappe_pg.compat.frappe import unique_insert_transaction

        def old_insert(self, *args, **kwargs):
            return "ok"

        for db_type, unique in (("postgres", False), ("mariadb", True)):
            with self.subTest(db_type=db_type, unique=unique):
                document = types.SimpleNamespace(db_insert=old_insert)
                doc = types.SimpleNamespace(
                    meta=types.SimpleNamespace(fields=[types.SimpleNamespace(unique=unique)])
                )
                fake_db = Mock(db_type=db_type)
                unique_insert_transaction._original_db_insert = None
                unique_insert_transaction._patched_db_insert = None
                with (
                    patch.object(unique_insert_transaction, "_load_base_document", return_value=document),
                    patch.object(frappe, "db", fake_db),
                ):
                    self.assertTrue(unique_insert_transaction.apply())
                    self.assertEqual(document.db_insert(doc), "ok")
                    fake_db.savepoint.assert_not_called()
