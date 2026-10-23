"""Matching what a person typed with LIKE / ILIKE, literally."""

#: The escape character every pattern built here declares: pass it as
#: `.ilike(pattern, escape=LIKE_ESCAPE)`.
LIKE_ESCAPE = "\\"


def contains_pattern(value: str) -> str:
    """`%value%`, with `%`, `_` and the escape character taken literally.

    Unescaped, `%` and `_` in typed text are wildcards: a filter for `a_b`
    also matches `axb`, and a lone `%` matches every row.
    """
    escaped = (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"
