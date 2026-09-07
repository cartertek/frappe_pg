import types
import unittest
from unittest.mock import patch

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
