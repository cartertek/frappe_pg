"""
Query Transformation Functions for PostgreSQL Compatibility
==========================================================

This module contains all SQL query transformation functions that convert
MySQL-specific syntax to PostgreSQL-compatible syntax.
"""

import re

from frappe_pg.utils.regex_patterns import (
    DATE_FORMAT_PATTERN,
    FORCE_INDEX_PATTERN,
    IF_FUNCTION_PATTERN,
    IFNULL_PATTERN,
    IGNORE_INDEX_PATTERN,
    USE_INDEX_PATTERN,
)

# ============================================================================
# Helper Functions
# ============================================================================


def split_by_comma(text):
    """
    Split text by commas, but respect parentheses and string literals.

    This is critical for parsing IF() function arguments correctly when
    they contain nested function calls or string literals with commas.

    Args:
        text: String to split

    Returns:
        List of parts split by top-level commas

    Example:
        >>> split_by_comma("a > 0, SUM(x, y), 'hello, world'")
        ['a > 0', ' SUM(x, y)', " 'hello, world'"]
    """
    parts = []
    current = []
    paren_depth = 0
    in_string = False
    string_char = None

    for i, char in enumerate(text):
        if char in ("'", '"'):
            if not in_string:
                in_string = True
                string_char = char
            elif char == string_char and (i == 0 or text[i - 1] != '\\'):
                in_string = False
                string_char = None
            current.append(char)
        elif in_string:
            current.append(char)
        elif char == '(':
            paren_depth += 1
            current.append(char)
        elif char == ')':
            paren_depth -= 1
            current.append(char)
        elif char == ',' and paren_depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)

    if current:
        parts.append(''.join(current))

    return parts


def _find_top_level_keyword(text, keyword, start=0):
    """Return the index of a SQL keyword outside strings and parentheses."""
    depth = 0
    quote = None
    i = start
    upper_keyword = keyword.upper()
    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        if char == "(":
            depth += 1
            i += 1
            continue
        if char == ")":
            depth = max(depth - 1, 0)
            i += 1
            continue
        if depth == 0 and text[i : i + len(keyword)].upper() == upper_keyword:
            before = text[i - 1] if i else " "
            after = text[i + len(keyword)] if i + len(keyword) < len(text) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return i
        i += 1
    return None


# ============================================================================
# Core Transformation Functions
# ============================================================================


def convert_if_to_case(query):
    """
    Convert MySQL IF() function to PostgreSQL CASE WHEN.

    MySQL's IF(condition, true_value, false_value) function doesn't exist in PostgreSQL.
    This function converts it to CASE WHEN condition THEN true_value ELSE false_value END.

    Handles:
    - Nested IF statements
    - IF inside aggregate functions (SUM, COUNT, etc.)
    - Complex conditions with parentheses
    - String literals in conditions or values
    - Multiple IF() calls in a single query (up to 100)

    Args:
        query: SQL query string that may contain IF() functions

    Returns:
        Query string with all IF() functions converted to CASE WHEN

    Examples:
        >>> convert_if_to_case("SELECT IF(a > 0, 1, 0)")
        "SELECT CASE WHEN a > 0 THEN 1 ELSE 0 END"

        >>> convert_if_to_case("SELECT SUM(IF(status='Active', amount, 0))")
        "SELECT SUM(CASE WHEN status='Active' THEN amount ELSE 0 END)"
    """
    if not IF_FUNCTION_PATTERN.search(query):
        return query

    max_iterations = 100  # Increased to handle queries with many IF() calls
    iteration = 0

    while IF_FUNCTION_PATTERN.search(query) and iteration < max_iterations:
        iteration += 1

        # Find the FIRST IF function (will keep finding new ones as we convert)
        match = IF_FUNCTION_PATTERN.search(query)
        if not match:
            break

        start_pos = match.start()

        # Check that this is actually a word boundary before IF
        # to avoid matching things like "DIFF(" or similar
        if start_pos > 0:
            prev_char = query[start_pos - 1]
            if prev_char.isalnum() or prev_char == '_':
                # This is part of another word like "DIFF(", skip it
                # Temporarily replace it to skip in this iteration
                query = query[:start_pos] + '___NOTIF___(' + query[match.end() :]
                continue

        if_start = match.end() - 1  # Position of opening parenthesis

        # Find matching closing parenthesis
        paren_count = 1
        pos = if_start + 1
        in_string = False
        string_char = None

        while pos < len(query) and paren_count > 0:
            char = query[pos]

            # Handle string literals
            if char in ("'", '"'):
                if not in_string:
                    in_string = True
                    string_char = char
                elif char == string_char and (pos == 0 or query[pos - 1] != '\\'):
                    in_string = False
                    string_char = None
            elif not in_string:
                if char == '(':
                    paren_count += 1
                elif char == ')':
                    paren_count -= 1

            pos += 1

        if paren_count != 0:
            # Malformed query, mark and skip
            query = query[:start_pos] + '___BADIF___(' + query[if_start + 1 :]
            continue

        # Extract the IF content
        if_end = pos
        if_content = query[if_start + 1 : if_end - 1]

        # Split by commas, respecting parentheses and strings
        parts = split_by_comma(if_content)

        if len(parts) != 3:
            # Invalid IF syntax, mark and skip
            query = query[:start_pos] + '___BADIF___(' + query[if_start + 1 :]
            continue

        condition = parts[0].strip()
        true_val = parts[1].strip()
        false_val = parts[2].strip()

        # Build CASE expression
        case_expr = f"CASE WHEN {condition} THEN {true_val} ELSE {false_val} END"

        # Replace in query
        query = query[:start_pos] + case_expr + query[if_end:]

    # Restore any marked patterns (these would be errors anyway, but keep original)
    query = query.replace('___NOTIF___(', 'IF(')
    query = query.replace('___BADIF___(', 'IF(')

    return query


def remove_index_hints(query):
    """
    Remove MySQL index hints (FORCE INDEX, USE INDEX, IGNORE INDEX).

    PostgreSQL doesn't support these MySQL-specific optimization hints.
    PostgreSQL's query planner automatically chooses the best index without hints.

    Args:
        query: SQL query string that may contain index hints

    Returns:
        Query string with all index hints removed

    Examples:
        >>> remove_index_hints("SELECT * FROM tab FORCE INDEX (idx_name)")
        "SELECT * FROM tab"

        >>> remove_index_hints("FROM tabGL Entry USE INDEX (posting_date)")
        "FROM tabGL Entry"
    """
    query = FORCE_INDEX_PATTERN.sub('', query)
    query = USE_INDEX_PATTERN.sub('', query)
    query = IGNORE_INDEX_PATTERN.sub('', query)
    return query


def convert_ifnull_to_coalesce(query):
    """
    Convert IFNULL(expr, default) to COALESCE(expr, default).

    While PostgreSQL supports COALESCE, it doesn't support MySQL's IFNULL function.
    Both functions have identical behavior: return the first non-NULL value.

    Args:
        query: SQL query string that may contain IFNULL() functions

    Returns:
        Query string with all IFNULL() converted to COALESCE()

    Examples:
        >>> convert_ifnull_to_coalesce("SELECT IFNULL(amount, 0)")
        "SELECT COALESCE(amount, 0)"
    """
    return IFNULL_PATTERN.sub('COALESCE(', query)


def convert_date_format(query):
    """
    Convert DATE_FORMAT(date, '%Y-%m-%d') to TO_CHAR(date, 'YYYY-MM-DD').

    MySQL's DATE_FORMAT uses different format specifiers than PostgreSQL's TO_CHAR.
    This currently only handles the most common format: %Y-%m-%d.

    Args:
        query: SQL query string that may contain DATE_FORMAT() functions

    Returns:
        Query string with DATE_FORMAT() converted to TO_CHAR()

    Examples:
        >>> convert_date_format("SELECT DATE_FORMAT(posting_date, '%Y-%m-%d')")
        "SELECT TO_CHAR(posting_date, 'YYYY-MM-DD')"

    TODO: Add support for more date format patterns
    """
    return DATE_FORMAT_PATTERN.sub(r"TO_CHAR(\1, 'YYYY-MM-DD')", query)


_QUOTED_IDENTIFIER = r'"(?:[^"]|"")+"(?:\."(?:[^"]|"")+")*'


def convert_mysql_date_arithmetic(query):
    """Convert common MySQL current-date arithmetic to PostgreSQL syntax.

    Handles ``CURDATE()`` and the simple ``DATE_SUB(expr, INTERVAL n unit)``
    form used by Frappe/ERPNext. Interval units are intentionally limited to
    day, week, month, and year, and the interval amount must be an integer.
    """
    date_sub = re.compile(
        r"\bDATE_SUB\(\s*(?P<expr>[^(),]+|CURDATE\(\))\s*,\s*"
        r"INTERVAL\s+(?P<amount>\d+)\s+(?P<unit>DAY|WEEK|MONTH|YEAR)\s*\)",
        re.IGNORECASE,
    )

    def replace_date_sub(match):
        expr = match.group("expr").strip()
        if re.fullmatch(r"CURDATE\(\)", expr, re.IGNORECASE):
            expr = "CURRENT_DATE"
        amount = match.group("amount")
        unit = match.group("unit").lower()
        return f"{expr} - INTERVAL '{amount} {unit}'"

    query = date_sub.sub(replace_date_sub, query)
    return re.sub(r"\bCURDATE\(\)", "CURRENT_DATE", query, flags=re.IGNORECASE)


def convert_mysql_datediff(query):
    """Translate simple MySQL DATEDIFF expressions to PostgreSQL date subtraction.

    MySQL DATEDIFF ignores time components and returns an integer day count.
    Cast both operands to DATE before subtraction so PostgreSQL has the same
    semantics even when an operand is a timestamp. Complex expressions are left
    untouched rather than parsed heuristically.
    """
    operand = r'(?:CURRENT_DATE|CURRENT_TIMESTAMP|NOW\(\)|(?:"[^"\r\n]+"|[A-Za-z_][A-Za-z0-9_$]*)(?:\.(?:"[^"\r\n]+"|[A-Za-z_][A-Za-z0-9_$]*))?|%\([A-Za-z_][A-Za-z0-9_]*\)s)'
    pattern = re.compile(
        rf'\bDATEDIFF\s*\(\s*(?P<left>{operand})\s*,\s*(?P<right>{operand})\s*\)',
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: f'(CAST({match.group("left")} AS DATE) - CAST({match.group("right")} AS DATE))',
        query,
    )


def convert_mysql_zero_date_sentinel(query):
    """Replace MySQL's zero-date lower bound when the SQL is explicitly date-shaped.

    MariaDB accepts ``0`` as a date lower bound. PostgreSQL does not. Restrict
    the rewrite to COALESCE expressions whose fallback is already an ISO-like
    date/datetime literal, which establishes the comparison as date-valued.
    """
    pattern = re.compile(
        r"(?P<expr>COALESCE\([^,]+,\s*'\d{4}-\d{2}-\d{2}(?: [^']*)?'\))" r"(?P<space>\s*>=\s*)'0(?:\.0+)?'",
        re.IGNORECASE,
    )
    return pattern.sub(r"\g<expr>\g<space>'0001-01-01 00:00:00'", query)


def normalize_erpnext_item_end_of_life_zero_date(query):
    """Translate ERPNext's legacy Item zero-date sentinel to PostgreSQL NULL semantics.

    ERPNext uses MySQL's ``0000-00-00`` value as an alternate "no end date"
    sentinel for ``Item.end_of_life``. PostgreSQL cannot represent that date.
    On PostgreSQL an unset end-of-life date is NULL, so only the recognizable
    ``end_of_life`` comparisons are normalized; unrelated zero-date literals
    are deliberately left untouched.
    """
    field = r'(?:(?:"tabItem"|tabItem)\.)?(?:"end_of_life"|end_of_life)'

    coalesced = re.compile(
        rf"COALESCE\(\s*(?P<field>{field})\s*,\s*'0000-00-00'\s*\)" r"\s*=\s*'0000-00-00'",
        re.IGNORECASE,
    )
    query = coalesced.sub(lambda match: f'{match.group("field")} IS NULL', query)

    direct = re.compile(
        rf"(?P<field>{field})\s*=\s*'0000-00-00'",
        re.IGNORECASE,
    )
    return direct.sub(lambda match: f'{match.group("field")} IS NULL', query)


def expand_mysql_having_alias(query):
    """Expand a simple SELECT alias referenced directly by HAVING.

    MySQL permits ``HAVING alias > value`` while PostgreSQL requires the
    underlying aggregate expression. This intentionally handles only a direct
    alias predicate so more complex HAVING expressions are left untouched.
    """
    select_match = re.search(r"\bSELECT\b(?P<select>.+?)\bFROM\b", query, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return query

    aliases = {}
    for item in split_by_comma(select_match.group("select")):
        alias_match = re.match(
            r"(?P<expr>.+?)\s+AS\s+(?P<alias>[A-Za-z_][A-Za-z0-9_$]*)\s*$",
            item.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if alias_match:
            aliases[alias_match.group("alias").lower()] = alias_match.group("expr").strip()

    if not aliases:
        return query

    having = re.compile(
        r"(?P<prefix>\bHAVING\s+)(?P<alias>[A-Za-z_][A-Za-z0-9_$]*)"
        r"(?P<space>\s*)(?P<operator><>|!=|<=|>=|=|<|>)",
        re.IGNORECASE,
    )

    def replace(match):
        expression = aliases.get(match.group("alias").lower())
        if expression is None:
            return match.group(0)
        return f'{match.group("prefix")}({expression}){match.group("space")}{match.group("operator")}'

    return having.sub(replace, query, count=1)


def remove_order_by_from_aggregate_only_query(query):
    """Drop ORDER BY from a single-row aggregate query with no GROUP BY.

    Frappe can carry a DocType's default ordering into Query Builder aggregate
    requests such as ``SELECT MAX(uid) ... ORDER BY creation DESC``. PostgreSQL
    rejects the non-aggregate ORDER BY expression, while the ordering cannot
    affect a query that returns one aggregate row.
    """
    if re.search(r"\bGROUP\s+BY\b", query, re.IGNORECASE):
        return query
    select_match = re.search(r"\bSELECT\b(?P<select>.+?)\bFROM\b", query, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return query

    aggregate = re.compile(r"^(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
    items = [item.strip() for item in split_by_comma(select_match.group("select"))]
    if not items or any(not aggregate.match(item) for item in items):
        return query

    return re.sub(
        r"\s+ORDER\s+BY\s+.+?(?=(?:\s+LIMIT\s+\d+)?\s*$)", "", query, flags=re.IGNORECASE | re.DOTALL
    )


def normalize_erpnext_v15_bom_group_query(query):
    """Make ERPNext v15's exploded-BOM aggregate query PostgreSQL-valid.

    ERPNext v15 groups exploded BOM rows by item code/stock UOM while selecting
    several functionally dependent columns and ordering by an ambiguous ``idx``
    alias. MariaDB accepts that permissive GROUP BY shape; PostgreSQL does not.
    ERPNext develop fixed this by qualifying/aggregating the same columns. Apply
    that semantics only to the recognizable exploded-BOM query shape.
    """
    markers = (
        r'\bFROM\s+"tabBOM Explosion Item"\s+bom_item\b',
        r'\bJOIN\s+"tabBOM"\s+bom\s+ON\s+bom_item\.parent\s*=\s*bom\.name',
        r'\bJOIN\s+"tabItem"\s+item\s+ON\s+item\.name\s*=\s*bom_item\.item_code',
        r'\bGROUP\s+BY\s+item_code\s*,\s*stock_uom\b',
        r'\bORDER\s+BY\s+idx\b',
        r'\bFROM\s+"tabBOM Item"\s+WHERE\s+item_code\s*=\s*bom_item\.item_code',
    )
    if any(not re.search(marker, query, re.IGNORECASE) for marker in markers):
        return query

    select_start = re.search(r"\bSELECT\b", query, re.IGNORECASE)
    if not select_start:
        return query
    from_start = _find_top_level_keyword(query, "FROM", select_start.end())
    if from_start is None:
        return query
    select_text = query[select_start.end() : from_start]

    aggregate_columns = {
        "bom_item.idx": "MIN(bom_item.idx) AS idx",
        "item.item_name": "MAX(item.item_name) AS item_name",
        "item.image": "MAX(item.image) AS image",
        "bom.project": "MAX(bom.project) AS project",
        "bom_item.rate": "MAX(bom_item.rate) AS rate",
        "item.item_group": "MAX(item.item_group) AS item_group",
        "item.allow_alternative_item": "MAX(item.allow_alternative_item) AS allow_alternative_item",
        "item_default.default_warehouse": "MAX(item_default.default_warehouse) AS default_warehouse",
        "item_default.expense_account as expense_account": "MAX(item_default.expense_account) AS expense_account",
        "item_default.buying_cost_center as cost_center": "MAX(item_default.buying_cost_center) AS cost_center",
        "bom_item.source_warehouse": "MAX(bom_item.source_warehouse) AS source_warehouse",
        "bom_item.operation": "MAX(bom_item.operation) AS operation",
        "bom_item.include_item_in_manufacturing": (
            "MAX(bom_item.include_item_in_manufacturing) AS include_item_in_manufacturing"
        ),
        "bom_item.description": "MAX(bom_item.description) AS description",
        "bom_item.sourced_by_supplier": "MAX(bom_item.sourced_by_supplier) AS sourced_by_supplier",
    }

    transformed_items = []
    for item in split_by_comma(select_text):
        stripped = item.strip()
        key = re.sub(r"\s+", " ", stripped).lower()
        replacement = aggregate_columns.get(key)
        if replacement is not None:
            transformed_items.append(replacement)
            continue

        if re.search(r"\bSUM\s*\(", stripped, re.IGNORECASE) and re.search(
            r"\bbom_item\.rate\b", stripped, re.IGNORECASE
        ):
            stripped = re.sub(
                r"\bbom_item\.rate\b",
                "MAX(bom_item.rate)",
                stripped,
                flags=re.IGNORECASE,
            )
        transformed_items.append(stripped)

    rebuilt_select = (
        query[select_start.start() : select_start.end()] + " " + ", ".join(transformed_items) + " "
    )
    query = query[: select_start.start()] + rebuilt_select + query[from_start:]
    query = re.sub(
        r"\bGROUP\s+BY\s+item_code\s*,\s*stock_uom\b",
        "GROUP BY bom_item.item_code, item.stock_uom",
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\bORDER\s+BY\s+idx\b",
        "ORDER BY MIN(bom_item.idx)",
        query,
        count=1,
        flags=re.IGNORECASE,
    )


def qualify_frappe_grouped_order_aggregate(query):
    """Restore the table qualifier lost by Frappe's PostgreSQL GROUP BY helper.

    DatabaseQuery.prepare_select_args() carries a qualified ORDER BY field into
    the SELECT list as ``MAX(modified) AS `tabDoctype.modified``` but strips the
    qualifier from the MAX argument. With joined tables that makes ``modified``
    ambiguous. The generated alias preserves the exact original qualified field,
    so use it to restore the qualifier without guessing which table is intended.
    """
    if not re.search(r'\bGROUP\s+BY\b', query, re.IGNORECASE):
        return query
    pattern = re.compile(
        r'\bMAX\s*\(\s*["`]?(?P<column>[A-Za-z_][A-Za-z0-9_]*)["`]?\s*\)'
        r'(?P<alias_space>\s+AS\s+)'
        r'(?P<quote>["`])(?P<table>tab[^"`]+)\.(?P=column)(?P=quote)',
        re.IGNORECASE,
    )

    def replace(match):
        column = match.group("column")
        quote = '"'
        alias_quote = match.group("quote")
        alias = f'{alias_quote}{match.group("table")}.{column}{alias_quote}'
        return f'MAX({quote}{match.group("table")}{quote}.{quote}{column}{quote}){match.group("alias_space")}{alias}'

    return pattern.sub(replace, query)


def normalize_erpnext_bom_items_grouping(query):
    """Make legacy ERPNext get_bom_items_as_dict queries PostgreSQL-valid.

    ERPNext v15/v16 build several raw-SQL BOM queries that group by bare
    ``item_code``/``stock_uom`` while joining BOM Item, BOM and Item tables.
    Besides making the group keys ambiguous, MariaDB permits many dependent
    columns outside the GROUP BY. ERPNext develop fixes this by qualifying the
    group keys, aggregating dependent scalar values, grouping semantic keys
    such as operation and the phantom-BOM pair, and ordering by an aggregate
    idx. Apply those rules only to the distinctive get_bom_items_as_dict shape.
    """
    markers = (
        r'\bFROM\s+"tabBOM(?: Explosion| Scrap| Secondary)? Item"\s+bom_item\b',
        r'\bJOIN\s+"tabBOM"\s+bom\s+ON\s+bom_item\.parent\s*=\s*bom\.name',
        r'\bJOIN\s+"tabItem"\s+item\s+ON\s+item\.name\s*=\s*bom_item\.item_code',
        r'\bGROUP\s+BY\s+item_code\b',
        r'\bSUM\s*\(.*?bom_item\.(?:stock_qty|qty)',
    )
    if any(not re.search(marker, query, re.IGNORECASE | re.DOTALL) for marker in markers):
        return query

    select_start = re.search(r"\bSELECT\b", query, re.IGNORECASE)
    if not select_start:
        return query
    from_start = _find_top_level_keyword(query, "FROM", select_start.end())
    if from_start is None:
        return query

    select_text = query[select_start.end() : from_start]
    selected_items = [item.strip() for item in split_by_comma(select_text)]

    # Qualify the legacy bare GROUP BY keys in whatever order that branch uses.
    # v15 uses item_code,stock_uom; v16 also has operation/operation_row_id and
    # secondary-item variants. Preserve those exact grouping dimensions rather
    # than inventing new partitions.
    group_match = re.search(
        r"\bGROUP\s+BY\s+(?P<keys>[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)"
        r"(?=\s+ORDER\s+BY\b|\s*$)",
        query,
        re.IGNORECASE,
    )
    if not group_match:
        return query
    key_tables = {
        "item_code": "bom_item",
        "stock_uom": "item",
        "operation": "bom_item",
        "operation_row_id": "bom_item",
        "secondary_item_type": "bom_item",
    }
    bare_keys = [key.strip().lower() for key in group_match.group("keys").split(",")]
    if any(key not in key_tables for key in bare_keys):
        return query
    group_keys = [f"{key_tables[key]}.{key}" for key in bare_keys]
    normalized_select = " ".join(selected_items).lower()
    has_phantom = re.search(r"\bbom_item\.is_phantom_item\b", normalized_select)
    has_bom_no = re.search(r"\bbom_item\.bom_no\b", normalized_select)
    if has_phantom and has_bom_no:
        group_keys.extend(["bom_item.bom_no", "bom_item.is_phantom_item"])

    simple_column = re.compile(
        r'^(?P<expr>(?:bom_item|bom|item|item_default)\.[A-Za-z_][A-Za-z0-9_]*)'
        r'(?P<alias>\s+(?:AS\s+)?(?:"?[A-Za-z_][A-Za-z0-9_]*"?))?$',
        re.IGNORECASE,
    )
    key_set = {key.lower() for key in group_keys}
    transformed_items = []
    for item in selected_items:
        match = simple_column.match(item)
        if match:
            expr = match.group("expr")
            if expr.lower() in key_set:
                transformed_items.append(item)
                continue
            # Keep bom_no/is_phantom_item paired as keys; for older query shapes
            # that expose only one of them, aggregate rather than inventing a key.
            aggregate = "MIN" if expr.lower() == "bom_item.idx" else "MAX"
            alias = match.group("alias") or f" AS {expr.rsplit('.', 1)[1]}"
            transformed_items.append(f"{aggregate}({expr}){alias}")
            continue

        if re.search(r"\bSUM\s*\(", item, re.IGNORECASE) and re.search(
            r"\bbom_item\.(?:rate|base_rate)\b", item, re.IGNORECASE
        ):
            item = re.sub(
                r"\bbom_item\.(rate|base_rate)\b",
                lambda match: f"MAX(bom_item.{match.group(1)})",
                item,
                flags=re.IGNORECASE,
            )
        transformed_items.append(item)

    rebuilt_select = (
        query[select_start.start() : select_start.end()] + " " + ", ".join(transformed_items) + " "
    )
    query = query[: select_start.start()] + rebuilt_select + query[from_start:]

    group_by = "GROUP BY " + ", ".join(group_keys)
    # Re-locate the clause after rebuilding SELECT: aggregation changes the
    # query length, so offsets captured from the original SQL are stale here.
    updated_group_match = re.search(
        r"\bGROUP\s+BY\s+[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*"
        r"(?=\s+ORDER\s+BY\b|\s*$)",
        query,
        re.IGNORECASE,
    )
    if not updated_group_match:
        return query
    query = query[: updated_group_match.start()] + group_by + query[updated_group_match.end() :]
    return re.sub(
        r"\bORDER\s+BY\s+idx\b",
        "ORDER BY MIN(bom_item.idx)",
        query,
        count=1,
        flags=re.IGNORECASE,
    )


def remove_erpnext_inventory_dimension_default_order(query):
    """Remove Frappe's implicit modified ordering from ERPNext's DISTINCT inventory-dimension query.

    ``get_inventory_dimensions`` asks for a DISTINCT projection without an
    explicit order. Frappe v15 injects its default ``modified DESC`` ordering,
    which PostgreSQL rejects because ``modified`` is not part of the DISTINCT
    projection. Restrict this rewrite to that exact ERPNext query shape so no
    intentional ordering is discarded elsewhere.
    """
    if not re.search(r'\bFROM\s+"tabInventory Dimension"', query, re.IGNORECASE):
        return query
    if not re.search(r'\bSELECT\s+DISTINCT\b', query, re.IGNORECASE):
        return query

    required_projection = (
        r'target_fieldname\s+as\s+fieldname',
        r'"source_fieldname"',
        r'"reference_document"\s+as\s+doctype',
        r'"validate_negative_stock"',
    )
    select_match = re.search(r'\bSELECT\b(?P<select>.+?)\bFROM\b', query, re.IGNORECASE | re.DOTALL)
    if not select_match or any(
        not re.search(pattern, select_match.group("select"), re.IGNORECASE) for pattern in required_projection
    ):
        return query

    return re.sub(
        r'\s+ORDER\s+BY\s+"tabInventory Dimension"\."modified"\s+DESC\s*$',
        '',
        query,
        flags=re.IGNORECASE,
    )


def convert_numeric_truthiness(query):
    """Convert bare numeric identifiers in boolean predicates to PostgreSQL booleans.

    MariaDB accepts numeric expressions directly in ``WHERE``/``AND``/``OR``
    predicates and ``CASE WHEN`` conditions, treating zero as false and non-zero as true. PostgreSQL requires
    an actual boolean expression. Frappe Query Builder can emit this shape when
    an application combines a numeric field directly with ``&``/``|``.

    This transformer intentionally handles only a bare quoted identifier or a
    qualified legacy alias/field used as a boolean predicate operand, plus a
    simple quoted or unquoted identifier used
    directly between ``CASE WHEN`` and ``THEN``. It does not attempt to infer the
    type of arbitrary SQL expressions.
    """
    case_operand = re.compile(
        rf'(?P<prefix>\bCASE\s+WHEN\b)'
        rf'(?P<space>\s*)(?P<identifier>{_QUOTED_IDENTIFIER}|[A-Za-z_][A-Za-z0-9_$]*)'
        rf'(?=(?P<trailing>\s*)\bTHEN\b)',
        re.IGNORECASE,
    )
    bare_qualified_identifier = r"[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*"
    operand = re.compile(
        rf'(?P<prefix>\bWHERE\b|\bHAVING\b|\bON\b|\bAND\b|\bOR\b|\()'
        rf'(?P<space>\s*)(?P<identifier>{_QUOTED_IDENTIFIER}|{bare_qualified_identifier})'
        rf'(?=(?P<trailing>\s*)(?P<suffix>\bAND\b|\bOR\b|\)|$))',
        re.IGNORECASE,
    )

    def replace_case_operand(match):
        identifier = match.group("identifier")
        if identifier.upper() in {"TRUE", "FALSE", "NULL"}:
            return match.group(0)
        return f'{match.group("prefix")}{match.group("space")}({identifier} <> 0)'

    query = case_operand.sub(replace_case_operand, query)

    def replace(match):
        # ``("name")`` is equally valid as a function/group argument and does
        # not establish boolean context. Require an adjacent AND/OR when the
        # only prefix is an opening parenthesis. This still covers Frappe HR's
        # ``("claimed_amount" AND "return_amount")`` query-builder output.
        if match.group("prefix") == "(" and match.group("suffix") == ")":
            return match.group(0)

        # In ``value BETWEEN lower AND upper`` the AND token is part of the
        # BETWEEN operator, not a boolean conjunction. Treating the upper bound
        # as a truthy numeric operand corrupts valid date/number ranges.
        if match.group("prefix").upper() == "AND":
            before = query[: match.start()]
            tail = re.split(r"\b(?:WHERE|HAVING|ON|OR|AND)\b|[()]", before, flags=re.IGNORECASE)[-1]
            if re.search(r"\bBETWEEN\b", tail, re.IGNORECASE):
                return match.group(0)

        return f'{match.group("prefix")}{match.group("space")}({match.group("identifier")} <> 0)'

    # Re-run until nested shapes such as ("claimed_amount" AND "return_amount")
    # are fully normalized. Replacements are idempotent because ``<> 0`` no
    # longer matches the bare-identifier lookahead.
    while True:
        transformed = operand.sub(replace, query)
        if transformed == query:
            return query
        query = transformed


def convert_mysql_double_quoted_literals(query):
    """Convert legacy MySQL double-quoted string literals to SQL strings.

    PostgreSQL treats double quotes as identifier delimiters. Two shapes are
    safe enough to distinguish from PostgreSQL column-to-column comparisons:

    * a right-hand token containing whitespace (for example ``"HR Settings"``);
    * an identifier-shaped right-hand token when the left-hand field is an
      unquoted legacy SQL identifier (for example ``name = "abc123"``).

    Frappe/Pypika-generated PostgreSQL column comparisons quote the field on
    both sides, so ``"paid_amount" = "return_amount"`` remains untouched.
    """
    whitespace_literal = re.compile(
        r'(?P<operator>=|<>|!=|<=|>=|<|>)' r'(?P<space>\s*)"(?P<value>[^"\r\n]*\s+[^"\r\n]*)"' r'(?!\s*\.)'
    )
    bare_field_literal = re.compile(
        r'(?P<field>(?<![.\w"])[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)'
        r'(?P<before>\s*)(?P<operator>=|<>|!=|<=|>=|<|>)(?P<after>\s*)'
        r'"(?P<value>[A-Za-z0-9_$@.:+/-]+)"(?!\s*\.)'
    )
    in_list = re.compile(
        r'(?P<field>(?<![.\w"])[A-Za-z_][A-Za-z0-9_$]*)' r'(?P<space>\s+IN\s*\()(?P<values>[^()]*)\)',
        re.IGNORECASE,
    )

    def replace_whitespace(match):
        value = match.group("value").replace("'", "''")
        return f'{match.group("operator")}{match.group("space")}\'{value}\''

    def replace_bare_field(match):
        # Raw MySQL SQL often uses backticks around a self-reference on the RHS
        # (``paid_amount = `paid_amount` + x``). Frappe's PostgreSQL modifier
        # converts those backticks to double quotes before this hook runs; keep
        # a same-name RHS as an identifier, not a string literal.
        if match.group("field").rsplit(".", 1)[-1].lower() == match.group("value").lower():
            return match.group(0)
        value = match.group("value").replace("'", "''")
        return (
            f'{match.group("field")}{match.group("before")}{match.group("operator")}'
            f'{match.group("after")}\'{value}\''
        )

    def replace_in_list(match):
        parts = [part.strip() for part in match.group("values").split(",")]
        if not parts or any(not re.fullmatch(r'"[^"\r\n]*"', part) for part in parts):
            return match.group(0)
        values = ", ".join("'" + part[1:-1].replace("'", "''") + "'" for part in parts)
        return f'{match.group("field")}{match.group("space")}{values})'

    query = whitespace_literal.sub(replace_whitespace, query)
    query = bare_field_literal.sub(replace_bare_field, query)
    return in_list.sub(replace_in_list, query)


def normalize_erpnext_negative_invoice_voucher_literal(query):
    """Quote ERPNext's negative-outstanding voucher type as a SQL string.

    Older ERPNext interpolates ``"Purchase Invoice"``/``"Sales Invoice"``
    into a SELECT projection, relying on MySQL's double-quoted string behavior.
    PostgreSQL reads it as an identifier. Restrict the rewrite to the exact
    negative-invoice query shape where the same value also names the source
    invoice table and the projection alias is ``voucher_type``.
    """
    for voucher_type in ("Purchase Invoice", "Sales Invoice"):
        if not re.search(rf'\bFROM\s+"tab{re.escape(voucher_type)}"', query, re.IGNORECASE):
            continue
        projection = re.compile(
            rf'"{re.escape(voucher_type)}"(?P<space>\s+)(?:AS\s+)?(?P<alias>"?voucher_type"?)\b',
            re.IGNORECASE,
        )
        return projection.sub(
            lambda match: f"'{voucher_type}'{match.group('space')}AS {match.group('alias')}",
            query,
            count=1,
        )
    return query


def normalize_payment_request_single_match_grouping(query):
    """Aggregate Payment Request name in ERPNext's single-match grouping query.

    ERPNext groups Payment Requests by reference tuple, selects ``name`` and
    ``COUNT(*)``, then keeps only groups whose count is one. MariaDB permits the
    non-grouped ``name`` projection; PostgreSQL does not. ``MIN(name)`` is
    equivalent for the only rows that survive the outer ``count = 1`` filter.
    """
    required = (
        r'\bFROM\s+"tabPayment Request"',
        r'COUNT\s*\(\s*\*\s*\)\s+(?:AS\s+)?"count"',
        r'GROUP\s+BY\s+"reference_doctype"\s*,\s*"reference_name"\s*,\s*"outstanding_amount"',
        r'WHERE\s+"sq0"\."count"\s*=\s*[\'"]?1[\'"]?',
    )
    if any(not re.search(pattern, query, re.IGNORECASE | re.DOTALL) for pattern in required):
        return query

    name_projection = re.compile(
        r'(?P<name>(?:"tabPayment Request"\.)?"name")\s+(?:AS\s+)?"payment_request"',
        re.IGNORECASE,
    )
    return name_projection.sub(r'MIN(\g<name>) "payment_request"', query, count=1)


def cast_timestamp_pattern_matches(query):
    """Cast Frappe timestamp fields to text for MySQL-style LIKE matching.

    MySQL permits LIKE against datetime values via implicit string coercion.
    PostgreSQL requires an explicit cast. Frappe's standard ``creation`` and
    ``modified`` fields are timestamps, so those two fields are safe to identify
    without application metadata.
    """
    pattern = re.compile(
        r'(?<![A-Za-z0-9_])(?P<field>(?:"[^"]+"\.)?"(?:creation|modified)")'
        r'(?P<space>\s+)(?P<op>I?LIKE)(?P<after>\s+)',
        re.IGNORECASE,
    )

    def replace(match):
        return f'CAST({match.group("field")} AS TEXT){match.group("space")}{match.group("op")}{match.group("after")}'

    return pattern.sub(replace, query)


def convert_mysql_inner_join_without_condition(query):
    """Translate MySQL INNER JOIN-without-condition into PostgreSQL CROSS JOIN.

    MySQL permits ``INNER JOIN table alias`` without ``ON``/``USING`` and
    treats it as a cross join. PostgreSQL requires a join condition for INNER
    JOIN. Restrict this to a joined table followed directly by a clause boundary
    so conditioned joins are never changed.
    """
    pattern = re.compile(
        r'\bINNER\s+JOIN\s+(?P<table>"(?:[^"]|"")+"(?:\."(?:[^"]|"")+")*)'
        r'(?P<alias>\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_$]*)?'
        r'(?P<space>\s+)(?P<next>WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION)\b',
        re.IGNORECASE,
    )

    def replace(match):
        return f'CROSS JOIN {match.group("table")}{match.group("alias") or ""}{match.group("space")}{match.group("next")}'

    return pattern.sub(replace, query)


def normalize_erpnext_production_plan_subitems_grouping(query):
    """Make ERPNext's grouped Production Plan sub-item query PostgreSQL-valid.

    ERPNext v16 groups BOM Item rows by ``item_code`` while selecting item,
    BOM, warehouse, UOM, and BOM-line attributes that MariaDB permits outside
    the GROUP BY. ERPNext develop now aggregates those dependent values, uses
    MIN for ``is_phantom_item``, and orders by MIN(idx). Apply the same
    semantics only to the recognizable Production Plan BOM Item query shape.
    """
    required = (
        r'\bFROM\s+"tabBOM Item"',
        r'\bJOIN\s+"tabBOM"',
        r'\bJOIN\s+"tabItem"',
        r'\bGROUP\s+BY\s+"tabBOM Item"\."item_code"',
        r'SUM\s*\(.*"tabBOM Item"\."stock_qty"',
    )
    if any(not re.search(pattern, query, re.IGNORECASE | re.DOTALL) for pattern in required):
        return query

    select_match = re.search(r"\bSELECT\b", query, re.IGNORECASE)
    if not select_match:
        return query
    from_start = _find_top_level_keyword(query, "FROM", select_match.end())
    if from_start is None:
        return query

    field_pattern = re.compile(
        r'^(?P<field>"(?P<table>tab(?:BOM Item|BOM|Item|Item Default|UOM Conversion Detail))"\.'
        r'"(?P<column>[^"]+)")'
        r'(?P<alias>\s+(?:AS\s+)?"[^"]+")?$',
        re.IGNORECASE,
    )
    grouped_field = '"tabBOM Item"."item_code"'
    transformed_items = []
    changed = False
    for item in split_by_comma(query[select_match.end() : from_start]):
        stripped = item.strip()
        match = field_pattern.match(stripped)
        if not match or match.group("field").lower() == grouped_field.lower():
            transformed_items.append(stripped)
            continue

        column = match.group("column")
        aggregate = (
            "MIN"
            if (match.group("table").lower() == "tabbom item" and column.lower() == "is_phantom_item")
            else "MAX"
        )
        alias = match.group("alias") or f' AS "{column}"'
        transformed_items.append(f'{aggregate}({match.group("field")}){alias}')
        changed = True

    if not changed:
        return query

    rebuilt = query[: select_match.end()] + " " + ", ".join(transformed_items) + " " + query[from_start:]
    return re.sub(
        r'\bORDER\s+BY\s+"tabBOM Item"\."idx"(?P<direction>\s+(?:ASC|DESC))?',
        lambda match: f'ORDER BY MIN("tabBOM Item"."idx"){match.group("direction") or ""}',
        rebuilt,
        count=1,
        flags=re.IGNORECASE,
    )


def normalize_erpnext_bank_clearance_journal_query(query):
    """Match ERPNext develop's PostgreSQL-safe Journal Entry bank-clearance query.

    ERPNext v15/v16 groups Journal Entry rows by account/name while selecting
    several functionally dependent columns, and also compares a Date field with
    MySQL's ``0000-00-00`` sentinel. Develop aggregates those dependent values
    with MAX and treats the zero date as NULL on PostgreSQL.
    """
    required = (
        r'\bFROM\s+"tabJournal Entry Account"',
        r'\bJOIN\s+"tabJournal Entry"',
        r'\bGROUP\s+BY\s+"tabJournal Entry Account"\."account"\s*,\s*"tabJournal Entry"\."name"',
        r'SUM\s*\(\s*"tabJournal Entry Account"\."debit_in_account_currency"\s*\)',
        r'SUM\s*\(\s*"tabJournal Entry Account"\."credit_in_account_currency"\s*\)',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query

    fields = {
        ("tabJournal Entry", "cheque_no"),
        ("tabJournal Entry", "cheque_date"),
        ("tabJournal Entry", "posting_date"),
        ("tabJournal Entry Account", "against_account"),
        ("tabJournal Entry", "clearance_date"),
        ("tabJournal Entry Account", "account_currency"),
    }
    select_match = re.search(r"\bSELECT\b(?P<select>.+?)\bFROM\b", query, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return query

    transformed_items = []
    simple_projection = re.compile(
        r'^(?P<expr>"(?P<table>[^"]+)"\."(?P<field>[^"]+)")'
        r'(?P<alias>\s+(?:AS\s+)?"?[A-Za-z_][A-Za-z0-9_]*"?)?$',
        re.IGNORECASE,
    )
    for item in split_by_comma(select_match.group("select")):
        stripped = item.strip()
        match = simple_projection.match(stripped)
        if match and (match.group("table"), match.group("field")) in fields:
            transformed_items.append(f'MAX({match.group("expr")}){match.group("alias") or ""}')
        else:
            # Existing aggregate projections are already PostgreSQL-safe and
            # must remain idempotent when this transformer runs repeatedly.
            transformed_items.append(stripped)

    query = (
        query[: select_match.start("select")]
        + ",".join(transformed_items)
        + query[select_match.end("select") :]
    )

    query = re.sub(
        r'(?P<field>"tabJournal Entry"\."clearance_date")\s*=\s*\'0000-00-00\'',
        r'\g<field> IS NULL',
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r'\bORDER\s+BY\s+(?P<field>"tabJournal Entry"\."posting_date")',
        lambda match: f'ORDER BY MAX({match.group("field")})',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    return query


def convert_erpnext_customer_suffix_unsigned(query):
    """Translate ERPNext's legacy Customer-name numeric suffix expression.

    v15 uses ``CAST(SUBSTRING_INDEX(name, ' ', -1) AS UNSIGNED)``. PostgreSQL
    has neither SUBSTRING_INDEX nor UNSIGNED. Mirror ERPNext develop: select the
    last whitespace-delimited token, keep its leading digits, NULL out an empty
    result, then cast to INTEGER. Restrict this to the Customer naming query.
    """
    if not re.search(r'\bFROM\s+"?tabCustomer"?\b', query, re.IGNORECASE):
        return query
    pattern = re.compile(
        r"CAST\s*\(\s*SUBSTRING_INDEX\s*\(\s*(?P<name>\"?name\"?)\s*,\s*' '\s*,\s*-1\s*\)\s+AS\s+UNSIGNED\s*\)",
        re.IGNORECASE,
    )

    def replace(match):
        name = match.group("name")
        return (
            "CAST(NULLIF(regexp_replace(regexp_replace("
            f"{name}, '^.*\\s', ''), '^(\\d*).*$', '\\1'), '') AS INTEGER)"
        )

    return pattern.sub(replace, query)


def normalize_erpnext_advance_payment_currency_aggregate(query):
    """Aggregate Advance Payment Ledger currency with the summed advance amount.

    ERPNext v15/v16 select ``ABS(SUM(amount)), currency`` without grouping.
    ERPNext develop uses ``MAX(currency)`` because the filtered ledger rows all
    represent the same account currency. Restrict the rewrite to that exact
    Advance Payment Ledger aggregate shape.
    """
    if not re.search(r'\bFROM\s+"tabAdvance Payment Ledger Entry"', query, re.IGNORECASE):
        return query
    if not re.search(r'ABS\s*\(\s*SUM\s*\(\s*"?amount"?\s*\)\s*\)', query, re.IGNORECASE):
        return query
    return re.sub(
        r'(?<![A-Za-z0-9_.])(?P<currency>(?:"tabAdvance Payment Ledger Entry"\.)?"?currency"?)'
        r'(?P<alias>\s+(?:AS\s+)?"?account_currency"?)',
        lambda match: f'MAX({match.group("currency")}){match.group("alias")}',
        query,
        count=1,
        flags=re.IGNORECASE,
    )


def normalize_erpnext_stock_voucher_group_order(query):
    """Backport ERPNext's PostgreSQL-safe stock-voucher ordering query.

    Older ERPNext groups Stock Ledger Entries by voucher while selecting and
    ordering by ungrouped posting fields. Develop selects only the voucher keys
    used downstream and orders each group by MIN(posting_datetime/creation).
    """
    required = (
        r'\bFROM\s+"tabStock Ledger Entry"',
        r'\bGROUP\s+BY\s+"?voucher_type"?\s*,\s*"?voucher_no"?',
        r'\bORDER\s+BY\s+"?posting_datetime"?',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query

    select_match = re.match(
        r'(?P<prefix>\s*SELECT\s+)(?P<select>.+?)(?P<from>\s+FROM\s+)', query, re.IGNORECASE | re.DOTALL
    )
    if not select_match:
        return query
    selected = select_match.group("select")
    if not all(
        re.search(rf'(?<![A-Za-z0-9_])"?{field}"?(?![A-Za-z0-9_])', selected, re.IGNORECASE)
        for field in ("voucher_type", "voucher_no", "posting_date", "posting_time", "creation")
    ):
        return query

    table_prefix = '"tabStock Ledger Entry".' if '"tabStock Ledger Entry".' in selected else ''
    replacement_select = f'{table_prefix}"voucher_type",{table_prefix}"voucher_no"'
    query = query[: select_match.start("select")] + replacement_select + query[select_match.end("select") :]
    query = re.sub(
        r'\bORDER\s+BY\s+(?P<field>(?:"tabStock Ledger Entry"\.)?"?posting_datetime"?)',
        lambda match: f'ORDER BY MIN({match.group("field")})',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r'(?P<comma>,\s*|\s+)ORDER\s+BY\s+(?P<field>(?:"tabStock Ledger Entry"\.)?"?creation"?)',
        lambda match: f'{match.group("comma")}ORDER BY MIN({match.group("field")})',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    return query


def normalize_erpnext_landed_cost_center_aggregate(query):
    """Match ERPNext's PostgreSQL-safe landed-cost aggregate.

    Older ERPNext selects ``SUM(applicable_charges), cost_center`` without a
    GROUP BY. MariaDB accepts that permissively; PostgreSQL does not. ERPNext
    develop fixes the query by aggregating ``cost_center`` with ``MAX``.
    Restrict the rewrite to the Landed Cost Item query shape.
    """
    if not re.search(r'\bFROM\s+"tabLanded Cost Item"', query, re.IGNORECASE):
        return query
    pattern = re.compile(
        r'(?P<sum>SUM\s*\(\s*(?P<prefix>(?:"tabLanded Cost Item"\.)?)"?applicable_charges"?\s*\))'
        r'(?P<comma>\s*,\s*)'
        r'(?P<center>(?P=prefix)"?cost_center"?)',
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: f'{match.group("sum")}{match.group("comma")}MAX({match.group("center")})',
        query,
        count=1,
    )


def convert_mysql_regexp_operator(query):
    """Translate MySQL's REGEXP predicate operator to PostgreSQL regex operators."""
    query = re.sub(r"\s+NOT\s+REGEXP\s+", " !~ ", query, flags=re.IGNORECASE)
    return re.sub(r"\s+REGEXP\s+", " ~ ", query, flags=re.IGNORECASE)


def convert_mysql_timestamp_pair(query):
    """Translate MySQL TIMESTAMP(date, time) for simple date/time expressions.

    MySQL's two-argument TIMESTAMP() adds the second temporal expression to the
    first. PostgreSQL expresses the same date+time operation with ``+``. Limit
    this transform to simple identifiers so one-argument casts/functions are
    never confused with this MySQL extension.
    """
    atom = r'(?:(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)\.)?"?[A-Za-z_][A-Za-z0-9_$]*"?'
    pattern = re.compile(
        rf"\bTIMESTAMP\s*\(\s*(?P<date>{atom})\s*,\s*(?P<time>{atom})\s*\)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: f'({match.group("date")} + {match.group("time")})', query)


def normalize_erpnext_repost_item_grouping(query):
    """Backport ERPNext's PostgreSQL-safe grouped Stock Ledger projection."""
    required = (
        r'\bFROM\s+"tabStock Ledger Entry"',
        r'\bGROUP\s+BY\s+"?item_code"?\s*,\s*"?warehouse"?',
        r'\bORDER\s+BY\s+(?:"tabStock Ledger Entry"\.)?"?creation"?\s+ASC',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query
    for field in ("posting_date", "posting_time", "creation", "posting_datetime"):
        pattern = re.compile(
            rf'(?<![A-Za-z0-9_])(?P<field>(?:"tabStock Ledger Entry"\.)?"{field}")'
            rf'(?=\s*(?:,|\s+FROM\b))',
            re.IGNORECASE,
        )
        query = pattern.sub(lambda match: f'MIN({match.group("field")}) AS "{field}"', query, count=1)
    query = re.sub(
        r'\bORDER\s+BY\s+(?P<field>(?:"tabStock Ledger Entry"\.)?"creation")\s+ASC',
        lambda match: f'ORDER BY MIN({match.group("field")}) ASC',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    return query


def normalize_erpnext_mode_of_payment_grouping(query):
    """Match ERPNext develop's PostgreSQL-safe Mode of Payment grouping."""
    required = (
        r'\bFROM\s+"tabMode of Payment Account"\s+mpa\s*,\s*"tabMode of Payment"\s+mp',
        r'\bmpa\.default_account\b',
        r'\bmpa\.parent\s+(?:AS\s+)?mop\b',
        r'\bmp\.type\s+(?:AS\s+)?type\b',
        r'\bGROUP\s+BY\s+mp\.name\b',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query
    return re.sub(
        r'\bGROUP\s+BY\s+mp\.name\b',
        "GROUP BY mpa.default_account, mpa.parent, mp.type",
        query,
        count=1,
        flags=re.IGNORECASE,
    )


def normalize_erpnext_budget_requested_amount(query):
    """Backport ERPNext's PostgreSQL-safe requested-budget aggregate.

    Older branches render ``SUM(stock_qty - ordered_qty) * rate``. PostgreSQL
    rejects the unaggregated ``rate`` and the expression is semantically wrong
    if grouped rows have different rates. ERPNext develop moved the rate inside
    SUM and NULL-protected it. Restrict this to Material Request Item queries.
    """
    if not re.search(r'\bFROM\s+"tabMaterial Request"', query, re.IGNORECASE):
        return query
    if not re.search(r'\bJOIN\s+"tabMaterial Request Item"', query, re.IGNORECASE):
        return query
    pattern = re.compile(
        r'(?P<sum>SUM\s*\(\s*(?P<diff>'
        r'COALESCE\s*\(\s*"tabMaterial Request Item"\."stock_qty"\s*,\s*0\s*\)\s*'
        r'-\s*COALESCE\s*\(\s*"tabMaterial Request Item"\."ordered_qty"\s*,\s*0\s*\)'
        r')\s*\))\s*\*\s*(?P<rate>"tabMaterial Request Item"\."rate")',
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: f'SUM(({match.group("diff")}) * COALESCE({match.group("rate")},0))',
        query,
        count=1,
    )


def normalize_erpnext_batch_availability_grouping(query):
    """Match ERPNext develop's grouped batch availability query on PostgreSQL."""
    required = (
        r'\bFROM\s+"tabStock Ledger Entry"',
        r'\bJOIN\s+"tabSerial and Batch Entry"',
        r'\bJOIN\s+"tabBatch"',
        r'\bGROUP\s+BY\s+"tabSerial and Batch Entry"\."batch_no"\s*,\s*'
        r'"tabSerial and Batch Entry"\."warehouse"',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query
    query = re.sub(
        r'(?<![A-Za-z0-9_])"tabBatch"\."expiry_date"(?=\s*(?:,|FROM\b))',
        'MAX("tabBatch"."expiry_date") AS "expiry_date"',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    for field in ("creation", "expiry_date"):
        query = re.sub(
            rf'(?P<prefix>\bORDER\s+BY\s+)"tabBatch"\."{field}"',
            rf'\g<prefix>MAX("tabBatch"."{field}")',
            query,
            flags=re.IGNORECASE,
        )
    return query


def normalize_erpnext_stock_ledger_batch_grouping(query):
    """Match ERPNext develop's grouped Stock Ledger batch query.

    Older ERPNext selects item_code and Batch expiry/creation fields outside a
    GROUP BY on batch_no + warehouse. Develop aggregates those dependent scalar
    values with MAX so PostgreSQL accepts the query without changing grouping.
    """
    required = (
        r'\bFROM\s+"tabStock Ledger Entry"',
        r'\bJOIN\s+"tabBatch"',
        r'SUM\s*\(\s*"tabStock Ledger Entry"\."actual_qty"\s*\)',
        r'\bGROUP\s+BY\s+"tabStock Ledger Entry"\."batch_no"\s*,\s*' r'"tabStock Ledger Entry"\."warehouse"',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query

    query = re.sub(
        r'(?<![A-Za-z0-9_])"tabStock Ledger Entry"\."item_code"(?=\s*(?:,|FROM\b))',
        'MAX("tabStock Ledger Entry"."item_code") AS "item_code"',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r'(?<![A-Za-z0-9_])"tabBatch"\."expiry_date"(?=\s*(?:,|FROM\b))',
        'MAX("tabBatch"."expiry_date") AS "expiry_date"',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    for field in ("creation", "expiry_date"):
        query = re.sub(
            rf'(?P<prefix>\bORDER\s+BY\s+)"tabBatch"\."{field}"',
            rf'\g<prefix>MAX("tabBatch"."{field}")',
            query,
            flags=re.IGNORECASE,
        )
    return query


def normalize_erpnext_unreconcile_payment_grouping(query):
    """Aggregate dependent Payment Ledger fields in unreconciliation queries.

    ERPNext develop wraps these per-group scalar values in MAX() while keeping
    the reference key as the GROUP BY column. Apply that exact semantics only
    to the Payment Ledger allocation query shape.
    """
    if not re.search(r'\bFROM\s+"tabPayment Ledger Entry"', query, re.IGNORECASE):
        return query
    if not re.search(
        r'ABS\s*\(\s*SUM\s*\(\s*"tabPayment Ledger Entry"\."amount_in_account_currency"\s*\)\s*\)',
        query,
        re.IGNORECASE,
    ):
        return query
    group_match = re.search(
        r'\bGROUP\s+BY\s+(?P<group>.+?)(?=\s+HAVING\b|\s+ORDER\s+BY\b|\s*$)', query, re.IGNORECASE
    )
    if not group_match:
        return query
    grouped = {part.strip().replace('"', '').lower() for part in group_match.group("group").split(",")}
    fields = (
        "company",
        "account",
        "party_type",
        "party",
        "voucher_type",
        "voucher_no",
        "against_voucher_type",
        "against_voucher_no",
        "account_currency",
    )
    for field in fields:
        canonical = f"tabpayment ledger entry.{field}"
        if canonical in grouped or field in grouped:
            continue
        projection = re.compile(
            rf'(?<![A-Za-z0-9_])(?P<expr>"tabPayment Ledger Entry"\."{field}")'
            rf'(?P<alias>\s+(?:AS\s+)?"?[A-Za-z_][A-Za-z0-9_]*"?)?'
            rf'(?=\s*,|\s+FROM\b)',
            re.IGNORECASE,
        )

        def replace_projection(match, field=field):
            alias = match.group("alias") or f' AS "{field}"'
            return f'MAX({match.group("expr")}){alias}'

        query = projection.sub(replace_projection, query, count=1)
    return query


def normalize_erpnext_reserved_warehouse_distinct(query):
    """Backport ERPNext's PostgreSQL-safe reserved-warehouse ordering.

    Older ERPNext selects DISTINCT warehouse and orders by the unselected
    creation timestamp. Develop uses GROUP BY warehouse + MIN(creation), which
    preserves distinct warehouses ordered by their earliest reservation.
    """
    required = (
        r'\bSELECT\s+DISTINCT\s+"tabStock Reservation Entry"\."warehouse"',
        r'\bFROM\s+"tabStock Reservation Entry"',
        r'\bORDER\s+BY\s+"tabStock Reservation Entry"\."creation"',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query
    query = re.sub(
        r'\bSELECT\s+DISTINCT\s+("tabStock Reservation Entry"\."warehouse")',
        r'SELECT \1',
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    order_match = re.search(
        r'\bORDER\s+BY\s+"tabStock Reservation Entry"\."creation"(?P<direction>\s+(?:ASC|DESC))?',
        query,
        re.IGNORECASE,
    )
    if not order_match:
        return query
    group = ' GROUP BY "tabStock Reservation Entry"."warehouse" '
    query = query[: order_match.start()] + group + query[order_match.start() :]
    return re.sub(
        r'\bORDER\s+BY\s+"tabStock Reservation Entry"\."creation"(?P<direction>\s+(?:ASC|DESC))?',
        lambda match: (
            'ORDER BY MIN("tabStock Reservation Entry"."creation")' + (match.group("direction") or "")
        ),
        query,
        count=1,
        flags=re.IGNORECASE,
    )


def remove_mysql_order_by_null(query):
    """Drop MySQL's ``ORDER BY NULL`` no-ordering idiom."""
    return re.sub(
        r"\s+ORDER\s+BY\s+NULL(?=\s*(?:LIMIT\b|OFFSET\b|FOR\b|$))",
        "",
        query,
        flags=re.IGNORECASE,
    )


def convert_mysql_limit_offset(query):
    """Translate MySQL ``LIMIT offset, count`` to PostgreSQL LIMIT/OFFSET."""
    value = r"(?:%\([A-Za-z_][A-Za-z0-9_]*\)s|%s|\d+)"
    pattern = re.compile(
        rf"\bLIMIT\s+(?P<offset>{value})\s*,\s*(?P<count>{value})",
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: f'LIMIT {match.group("count")} OFFSET {match.group("offset")}',
        query,
    )


def convert_mysql_update_join(query):
    """Convert the simple MySQL ``UPDATE ... JOIN`` form to PostgreSQL ``FROM``.

    Handles the single-inner-join form emitted by Frappe Query Builder and used
    by application migration patches. More complex multi-join UPDATE statements
    are intentionally left unchanged rather than guessed at.
    """
    pattern = re.compile(
        rf'^\s*UPDATE\s+(?P<target>{_QUOTED_IDENTIFIER})\s+(?P<target_alias>"[^"]+")\s+'
        rf'JOIN\s+(?P<joined>{_QUOTED_IDENTIFIER})\s+(?P<joined_alias>"[^"]+")\s+'
        r'ON\s+(?P<join_condition>.+?)\s+SET\s+(?P<set_clause>.+?)\s+WHERE\s+(?P<where_clause>.+?)\s*;?\s*$',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.match(query)
    if not match or re.search(r"\bJOIN\b", match.group("join_condition"), re.IGNORECASE):
        return query

    target_alias = match.group("target_alias")
    set_clause = re.sub(
        r'(?<![\w"])' + re.escape(target_alias) + r'\.',
        '',
        match.group("set_clause"),
    )
    return (
        f'UPDATE {match.group("target")} AS {target_alias} '
        f'SET {set_clause} FROM {match.group("joined")} AS {match.group("joined_alias")} '
        f'WHERE {match.group("join_condition")} AND {match.group("where_clause")}'
    )


def apply_all_query_transformations(query):
    """
    Apply all query transformations in the correct order.

    This is the main entry point for query transformation. It applies all
    conversion functions in a specific order to ensure they don't interfere
    with each other.

    Order of transformations:
    1. Remove index hints (simple string removal)
    2. Convert IF() to CASE WHEN (complex, must be done before other conversions)
    3. Convert IFNULL to COALESCE (simple replacement)
    4. Convert DATE_FORMAT to TO_CHAR (simple replacement)
    5. Convert MySQL current-date arithmetic
    6. Normalize explicit MySQL zero-date sentinels
    7. Normalize ERPNext Item zero-date sentinel semantics
    8. Expand simple MySQL HAVING aliases
    9. Remove irrelevant ORDER BY from aggregate-only single-row queries
    10. Normalize ERPNext v15 exploded-BOM GROUP BY semantics
    11. Remove ERPNext inventory-dimension implicit ordering under DISTINCT
    12. Convert MySQL numeric truthiness in boolean predicates
    13. Convert unambiguous double-quoted string literals
    14. Convert simple MySQL UPDATE ... JOIN statements

    Args:
        query: SQL query string

    Returns:
        Transformed query string compatible with PostgreSQL

    Note:
        This function also includes debug logging to detect unconverted IF()
        functions, which can help identify edge cases that need handling.
    """
    if not isinstance(query, str):
        return query

    original_query = query

    # Order matters here!
    query = remove_index_hints(query)
    query = convert_if_to_case(query)
    query = convert_ifnull_to_coalesce(query)
    query = convert_date_format(query)
    query = convert_mysql_date_arithmetic(query)
    query = convert_mysql_datediff(query)
    query = convert_mysql_zero_date_sentinel(query)
    query = normalize_erpnext_item_end_of_life_zero_date(query)
    query = expand_mysql_having_alias(query)
    query = remove_order_by_from_aggregate_only_query(query)
    query = remove_mysql_order_by_null(query)
    query = convert_mysql_limit_offset(query)
    query = normalize_erpnext_v15_bom_group_query(query)
    query = normalize_erpnext_bom_items_grouping(query)
    query = qualify_frappe_grouped_order_aggregate(query)
    query = remove_erpnext_inventory_dimension_default_order(query)
    query = convert_numeric_truthiness(query)
    query = convert_mysql_double_quoted_literals(query)
    query = convert_mysql_regexp_operator(query)
    query = convert_mysql_timestamp_pair(query)
    query = normalize_erpnext_negative_invoice_voucher_literal(query)
    query = normalize_payment_request_single_match_grouping(query)
    query = cast_timestamp_pattern_matches(query)
    query = convert_mysql_inner_join_without_condition(query)
    query = normalize_erpnext_production_plan_subitems_grouping(query)
    query = normalize_erpnext_bank_clearance_journal_query(query)
    query = convert_erpnext_customer_suffix_unsigned(query)
    query = normalize_erpnext_advance_payment_currency_aggregate(query)
    query = normalize_erpnext_stock_voucher_group_order(query)
    query = normalize_erpnext_repost_item_grouping(query)
    query = normalize_erpnext_mode_of_payment_grouping(query)
    query = normalize_erpnext_landed_cost_center_aggregate(query)
    query = normalize_erpnext_budget_requested_amount(query)
    query = normalize_erpnext_batch_availability_grouping(query)
    query = normalize_erpnext_stock_ledger_batch_grouping(query)
    query = normalize_erpnext_unreconcile_payment_grouping(query)
    query = normalize_erpnext_reserved_warehouse_distinct(query)
    query = convert_mysql_update_join(query)

    # Debug: Log if IF() is still present after transformation
    if 'IF(' in query.upper():
        import re

        print("\n" + "=" * 80)
        print("⚠️  WARNING: IF() still present after transformation!")
        print("=" * 80)
        print(f"Original query length: {len(original_query)} chars")
        print(f"Transformed query length: {len(query)} chars")
        print(f"Original query snippet: {original_query[:300]}...")
        print(f"Transformed query snippet: {query[:300]}...")

        # Find all IF( occurrences
        if_positions = [m.start() for m in re.finditer(r'\bIF\s*\(', query, re.IGNORECASE)]
        print(f"IF() found at {len(if_positions)} positions: {if_positions[:10]}...")  # Show first 10

        # Show context around first unconverted IF
        if if_positions:
            first_if = if_positions[0]
            context_start = max(0, first_if - 50)
            context_end = min(len(query), first_if + 100)
            print("\nFirst unconverted IF() context:")
            print(f"...{query[context_start:context_end]}...")
        print("=" * 80 + "\n")

    return query
