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

    This transformer intentionally handles only a bare quoted identifier used as
    a boolean predicate operand, plus a simple quoted or unquoted identifier used
    directly between ``CASE WHEN`` and ``THEN``. It does not attempt to infer the
    type of arbitrary SQL expressions.
    """
    case_operand = re.compile(
        rf'(?P<prefix>\bCASE\s+WHEN\b)'
        rf'(?P<space>\s*)(?P<identifier>{_QUOTED_IDENTIFIER}|[A-Za-z_][A-Za-z0-9_$]*)'
        rf'(?=(?P<trailing>\s*)\bTHEN\b)',
        re.IGNORECASE,
    )
    operand = re.compile(
        rf'(?P<prefix>\bWHERE\b|\bHAVING\b|\bON\b|\bAND\b|\bOR\b|\()'
        rf'(?P<space>\s*)(?P<identifier>{_QUOTED_IDENTIFIER})'
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
        return f'{match.group("prefix")}{match.group("space")}' f'({match.group("identifier")} <> 0)'

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

    query = whitespace_literal.sub(replace_whitespace, query)
    query = bare_field_literal.sub(replace_bare_field, query)
    return in_list.sub(replace_in_list, query)


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
    7. Expand simple MySQL HAVING aliases
    8. Remove irrelevant ORDER BY from aggregate-only single-row queries
    9. Remove ERPNext inventory-dimension implicit ordering under DISTINCT
    10. Convert MySQL numeric truthiness in boolean predicates
    11. Convert unambiguous double-quoted string literals
    12. Convert simple MySQL UPDATE ... JOIN statements

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
    query = expand_mysql_having_alias(query)
    query = remove_order_by_from_aggregate_only_query(query)
    query = remove_erpnext_inventory_dimension_default_order(query)
    query = convert_numeric_truthiness(query)
    query = convert_mysql_double_quoted_literals(query)
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
