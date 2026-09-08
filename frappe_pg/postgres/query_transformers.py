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
        # BETWEEN operator, not a boolean conjunction. Treating a numeric-looking
        # upper bound as a boolean operand corrupts valid date/number ranges (and
        # produced ``BETWEEN from_date AND (to_date <> 0)`` in HRMS).
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
        r'(?P<field>(?<![.\w"])[A-Za-z_][A-Za-z0-9_$]*)'
        r'(?P<before>\s*)(?P<operator>=|<>|!=|<=|>=|<|>)(?P<after>\s*)'
        r'"(?P<value>[A-Za-z0-9_$@.:+/-]+)"(?!\s*\.)'
    )
    like_literal = re.compile(
        r'(?P<operator>\b(?:LIKE|NOT\s+LIKE)\b)(?P<space>\s*)"(?P<value>[^"\r\n]*%[^"\r\n]*)"',
        re.IGNORECASE,
    )
    in_list = re.compile(
        r'(?P<field>(?<![.\w"])[A-Za-z_][A-Za-z0-9_$]*)' r'(?P<space>\s+IN\s*\()(?P<values>[^()]*)\)',
        re.IGNORECASE,
    )

    def replace_whitespace(match):
        value = match.group("value").replace("'", "''")
        return f'{match.group("operator")}{match.group("space")}\'{value}\''

    def replace_bare_field(match):
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

    def replace_like_literal(match):
        value = match.group("value")
        # MySQL code sometimes embeds a DB-API placeholder inside the quoted
        # LIKE pattern, e.g. ``LIKE "%%%s%%"``. Simply changing the quote
        # characters would leave the placeholder inside a SQL string and
        # psycopg would produce invalid SQL (``'%'value'%'``). Preserve the
        # wildcard semantics with PostgreSQL concatenation instead.
        if value == "%%%s%%":
            return f"{match.group('operator')}{match.group('space')}'%%' || %s || '%%'"
        value = value.replace("'", "''")
        return f"{match.group('operator')}{match.group('space')}'{value}'"

    query = whitespace_literal.sub(replace_whitespace, query)
    query = bare_field_literal.sub(replace_bare_field, query)
    query = like_literal.sub(replace_like_literal, query)
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
        r'\bFROM\s+"tabPayment Request"\b',
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


def normalize_hrms_shift_assignment_empty_end_date(query):
    """Treat HRMS Shift Assignment empty end dates as NULL on PostgreSQL.

    MariaDB tolerates comparing a Date column to the empty string. PostgreSQL
    does not. Restrict the rewrite to HRMS's Shift Assignment ``end_date``
    predicates, where the application already treats NULL and empty as the same
    open-ended value.
    """
    if not re.search(r'\bFROM\s+"tabShift Assignment"\b', query, re.IGNORECASE):
        return query
    pattern = re.compile(
        r'(?P<field>(?:"tabShift Assignment"\.)?"end_date")\s*=\s*\'\'',
        re.IGNORECASE,
    )
    return pattern.sub(r'\g<field> IS NULL', query)


def normalize_hrms_skill_assessment_group_order(query):
    """Aggregate HRMS Skill Assessment idx when ordering a grouped rating query."""
    required = (
        r'\bFROM\s+"tabSkill Assessment"\b',
        r'AVG\s*\(\s*"tabSkill Assessment"\."rating"\s*\)',
        r'GROUP\s+BY\s+"tabSkill Assessment"\."skill"',
        r'ORDER\s+BY\s+"tabSkill Assessment"\."idx"',
    )
    if any(not re.search(pattern, query, re.IGNORECASE) for pattern in required):
        return query
    return re.sub(
        r'ORDER\s+BY\s+("tabSkill Assessment"\."idx")',
        r'ORDER BY MIN(\1)',
        query,
        count=1,
        flags=re.IGNORECASE,
    )


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
    query = convert_mysql_zero_date_sentinel(query)
    query = normalize_erpnext_item_end_of_life_zero_date(query)
    query = expand_mysql_having_alias(query)
    query = remove_order_by_from_aggregate_only_query(query)
    query = normalize_erpnext_v15_bom_group_query(query)
    query = remove_erpnext_inventory_dimension_default_order(query)
    query = convert_numeric_truthiness(query)
    query = convert_mysql_double_quoted_literals(query)
    query = normalize_erpnext_negative_invoice_voucher_literal(query)
    query = normalize_payment_request_single_match_grouping(query)
    query = cast_timestamp_pattern_matches(query)
    query = convert_mysql_inner_join_without_condition(query)
    query = normalize_hrms_shift_assignment_empty_end_date(query)
    query = normalize_hrms_skill_assessment_group_order(query)
    query = normalize_erpnext_landed_cost_center_aggregate(query)
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
