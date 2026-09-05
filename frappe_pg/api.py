"""
API endpoints for frappe_pg app
"""

import frappe


@frappe.whitelist(allow_guest=False)
def reload_patches():
	"""
	Reload PostgreSQL patches - useful for development/debugging
	"""
	from frappe_pg.postgres.database_patches import apply_postgres_fixes, remove_postgres_fixes

	remove_postgres_fixes()
	apply_postgres_fixes()

	return {"success": True, "message": "PostgreSQL patches reloaded successfully"}


@frappe.whitelist(allow_guest=False)
def test_conversion():
	"""
	Test query conversion without executing
	"""
	from frappe_pg.postgres.query_transformers import convert_if_to_case, remove_index_hints

	test_queries = [
		"SELECT SUM(IF(amount > 0, amount, 0)) FROM table",
		"SELECT * FROM tabGL Entry FORCE INDEX (posting_date) WHERE date = '2024-01-01'",
		"SELECT IFNULL(name, 'N/A') FROM tabItem",
	]

	results = []
	for query in test_queries:
		transformed = query
		transformed = remove_index_hints(transformed)
		transformed = convert_if_to_case(transformed)

		results.append({"original": query, "transformed": transformed})

	return {"success": True, "tests": results}


@frappe.whitelist(allow_guest=False)
def check_patches_status():
	"""
	Check if patches are applied
	"""
	import inspect

	from frappe.database.postgres.database import PostgresDatabase

	try:
		source = inspect.getsource(PostgresDatabase._transform_query)
		is_patched = "apply_all_query_transformations" in source

		method_info = {
			"function_name": PostgresDatabase._transform_query.__name__,
			"module": PostgresDatabase._transform_query.__module__,
			"is_patched": is_patched,
			"sql_module": PostgresDatabase.sql.__module__,
		}

		# Check database functions
		db_functions_status = {}

		try:
			result = frappe.db.sql("SELECT GROUP_CONCAT(name::text) FROM (VALUES ('a'), ('b')) AS t(name)")
			db_functions_status["GROUP_CONCAT"] = "Working" if result[0][0] == "a,b" else "Failed"
		except Exception as e:
			db_functions_status["GROUP_CONCAT"] = f"Error: {str(e)[:50]}"

		try:
			result = frappe.db.sql("SELECT unix_timestamp('2024-01-01'::timestamp)")
			db_functions_status["unix_timestamp"] = "Working" if isinstance(result[0][0], int) else "Failed"
		except Exception as e:
			db_functions_status["unix_timestamp"] = f"Error: {str(e)[:50]}"

		return {
			"success": True,
			"patches_applied": is_patched,
			"method_info": method_info,
			"database_functions": db_functions_status,
			"db_type": type(frappe.db).__name__,
		}
	except Exception as e:
		return {"success": False, "error": str(e)}
