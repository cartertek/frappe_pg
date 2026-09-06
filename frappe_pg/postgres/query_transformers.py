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


def convert_numeric_truthiness(query):
    """Convert bare numeric identifiers in boolean predicates to PostgreSQL booleans.

    MariaDB accepts numeric expressions directly in ``WHERE``/``AND``/``OR``
    predicates, treating zero as false and non-zero as true. PostgreSQL requires
    an actual boolean expression. Frappe Query Builder can emit this shape when
    an application combines a numeric field directly with ``&``/``|``.

    This transformer intentionally handles only a bare quoted identifier used as
    a boolean operand. It does not attempt to infer the type of arbitrary SQL
    expressions.
    """
    operand = re.compile(
        rf'(?P<prefix>\bWHERE\b|\bHAVING\b|\bON\b|\bAND\b|\bOR\b|\()'
        rf'(?P<space>\s*)(?P<identifier>{_QUOTED_IDENTIFIER})'
        rf'(?=\s*(?:\bAND\b|\bOR\b|\)|$))',
        re.IGNORECASE,
    )

    # Re-run until nested shapes such as ("claimed_amount" AND "return_amount")
    # are fully normalized. Replacements are idempotent because ``<> 0`` no
    # longer matches the bare-identifier lookahead.
    while True:
        transformed, count = operand.subn(
            lambda match: (
                f'{match.group("prefix")}{match.group("space")}' f'({match.group("identifier")} <> 0)'
            ),
            query,
        )
        query = transformed
        if not count:
            return query


def convert_mysql_double_quoted_literals(query):
    """Convert unambiguous MySQL double-quoted string literals to SQL strings.

    PostgreSQL treats double quotes as identifier delimiters. Frappe field names
    do not contain spaces, so a double-quoted token containing whitespace on the
    right side of a predicate is unambiguously a legacy MySQL string literal in
    the application SQL we need to support (for example ``doctype = "HR Settings"``).

    Deliberately leave identifier-shaped values such as ``"other_column"``
    untouched because those may be real PostgreSQL identifiers.
    """
    literal = re.compile(
        r'(?P<operator>=|<>|!=|<=|>=|<|>)' r'(?P<space>\s*)"(?P<value>[^"\r\n]*\s+[^"\r\n]*)"'
    )

    def replace(match):
        value = match.group("value").replace("'", "''")
        return f'{match.group("operator")}{match.group("space")}\'{value}\''

    return literal.sub(replace, query)


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
    5. Convert MySQL numeric truthiness in boolean predicates
    6. Convert unambiguous double-quoted string literals
    7. Convert simple MySQL UPDATE ... JOIN statements

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
