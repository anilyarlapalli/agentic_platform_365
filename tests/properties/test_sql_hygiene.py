"""Static checks on the SQL this codebase writes.

Two patterns have each cost debugging time more than once. Both are invisible
until the statement runs, and one only fails on a code path a test happens to
reach — so they are checked statically rather than discovered again.

This is a cheap category of test that is easy to dismiss as pedantry. The
justification is empirical: the first pattern below appeared **three times** in
this repository during a single build.

## Why this parses rather than greps

The first version scanned lines with a regex and immediately failed — on the
docstrings that *describe* the anti-pattern. A line-based scanner cannot tell
`SET app.tenant_id = '<uuid>'` written as an example of what not to do from the
same text written as executable SQL.

So it walks the AST and inspects only **non-docstring string literals**, which
is where SQL actually lives. Comments and documentation are structurally
excluded rather than filtered by heuristic, which means the rule can be
documented honestly in the module it constrains.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.property

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_DIRS = ("platform_core", "apps", "workloads")

# `:name::type` — a Postgres cast immediately after a bind parameter.
#
# SQLAlchemy's `text()` claims the first colon of the `::`, so the parameter is
# parsed as `name:` and Postgres receives a syntax error. Seen with
# `:workload::text` in the lease query and `:turn::jsonb` in the session append.
# The fix is always `CAST(:name AS type)`.
BIND_THEN_CAST = re.compile(r":[A-Za-z_][A-Za-z0-9_]*::")

# A `SET` statement targeting an application GUC. Postgres does not accept bind
# parameters in `SET`, so the statement form forces a choice between a syntax
# error and interpolating a caller-supplied value into the statement body — on
# the tenant discriminator, which is the control everything else depends on.
SET_APP_GUC = re.compile(r"\bSET\s+(?:LOCAL\s+)?app\.", re.IGNORECASE)



def _source_files() -> list[Path]:
    files: list[Path] = []
    for directory in SOURCE_DIRS:
        files.extend((ROOT / directory).rglob("*.py"))
    return sorted(f for f in files if "__pycache__" not in f.parts)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of string constants that are docstrings, so they can be skipped."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return docstrings


def _sql_literals(path: Path) -> list[tuple[int, str]]:
    """Every non-docstring string literal in a file, with its line number.

    Comments never appear — they are not part of the AST at all — so
    documentation describing an anti-pattern cannot trip a rule about it.
    """
    tree = ast.parse(path.read_text())
    skip = _docstring_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def test_no_bind_parameter_is_followed_by_a_cast(record_evidence):
    """`:name::type` is a syntax error that only appears at execution.

    Worse than a plain bug: the obvious workaround is to interpolate the value
    into the SQL string instead, which turns a syntax error into an injection
    point. That is exactly the choice avoided in the tenant GUC.
    """
    offenders: list[str] = []
    files = _source_files()
    for path in files:
        for lineno, literal in _sql_literals(path):
            if BIND_THEN_CAST.search(literal):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno}: {literal.strip()[:90]}"
                )

    assert not offenders, (
        "a bind parameter is immediately followed by a `::` cast. SQLAlchemy claims "
        "the first colon, so the parameter name is wrong and Postgres sees a syntax "
        "error. Use CAST(:name AS type).\n" + "\n".join(offenders)
    )

    record_evidence(
        "sql_no_bind_cast_collision", holds=True, files_scanned=len(files),
        detail="no `:param::type` in any executable string literal",
    )


def test_application_gucs_use_set_config(record_evidence):
    """`SET LOCAL x = :y` cannot bind, and interpolating it is an injection point."""
    offenders: list[str] = []
    files = _source_files()
    for path in files:
        for lineno, literal in _sql_literals(path):
            if SET_APP_GUC.search(literal) and "set_config" not in literal:
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno}: {literal.strip()[:90]}"
                )

    assert not offenders, (
        "use set_config(name, value, is_local) rather than a SET statement for "
        "application GUCs — SET cannot take bind parameters:\n" + "\n".join(offenders)
    )

    record_evidence(
        "sql_gucs_use_set_config", holds=True,
        detail="application GUCs are set through set_config, so values bind safely",
    )


def test_the_checks_would_catch_a_real_violation(tmp_path, record_evidence):
    """The scanner must detect the patterns in code and ignore them in prose.

    Without this the whole file could pass by scanning nothing — the vacuity
    failure this codebase has hit repeatedly. So: a file containing both a real
    violation and a docstring describing one, asserting the scanner sees exactly
    the first.
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""Never write `:tenant::uuid` — this docstring must not trip the rule."""\n'
        "\n"
        "def bad():\n"
        "    # A comment mentioning SET app.tenant_id = x must also be ignored.\n"
        '    return text("SELECT * FROM t WHERE id = :tenant::uuid")\n'
        "\n"
        "def also_bad():\n"
        '    return text("SET LOCAL app.tenant_id = :t")\n'
    )

    literals = _sql_literals(sample)
    cast_hits = [lit for _, lit in literals if BIND_THEN_CAST.search(lit)]
    guc_hits = [
        lit for _, lit in literals
        if SET_APP_GUC.search(lit) and "set_config" not in lit
    ]

    assert len(cast_hits) == 1, f"expected exactly the code violation, saw {cast_hits}"
    assert "SELECT * FROM t" in cast_hits[0]
    assert len(guc_hits) == 1, guc_hits
    # The module docstring mentions the pattern and must not be counted.
    assert not any("docstring must not trip" in lit for lit in cast_hits)

    record_evidence(
        "sql_hygiene_scanner_is_effective", holds=True,
        detail="detects violations in string literals; ignores docstrings and comments",
    )
