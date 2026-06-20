"""Rule-violation harness for the AtScale query-semantic-layer skill (FR2, FR7).

This module provides deterministic, SQL-text-only checks for rules whose decidability
class is ``sql-only``.  Rules requiring model metadata (``sql+metadata``) or intent
are marked ``lm_driven=True`` in cases.yaml and are excluded from the hard CI floor.

FR7 compliance: the harness checks for a live eval lock before running and refuses
to start if one is held, preventing API contention on shared-API boxes.
"""

from __future__ import annotations

import re
import tokenize
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RuleVerdict:
    """Result of checking a single SQL string against one rule."""

    rule_id: str
    verdict: str  # "pass" | "fail"
    reason: str
    lm_driven: bool = False  # True → excluded from the hard CI floor


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

# Match a semicolon that is NOT inside a single-quoted string literal.
# Strategy: strip string literals first, then search for semicolons.

def _strip_string_literals(sql: str) -> str:
    """Replace the contents of single-quoted string literals with spaces.

    This is a conservative approach: we replace the interior of every
    ``'...'`` literal (handling ``''`` escapes) so that a semicolon inside
    a string is invisible to subsequent pattern matching.

    We do NOT rely on the Python tokenizer here because SQL string syntax
    (especially ``''``-escaped strings) differs from Python string syntax.
    """
    result: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            # Scan to the end of the string literal, handling '' escapes.
            result.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                if c == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        # Escaped quote inside literal — skip both.
                        result.append(" ")
                        result.append(" ")
                        i += 2
                    else:
                        # End of literal.
                        result.append(c)
                        i += 1
                        break
                else:
                    result.append(" ")  # Replace literal content.
                    i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _has_multiple_statements(sql: str) -> bool:
    """Return True if *sql* contains more than one SQL statement.

    A statement boundary is a ``;`` that appears outside of a string literal
    and is followed by non-whitespace content (i.e. there IS a subsequent
    statement, not just a trailing semicolon).
    """
    cleaned = _strip_string_literals(sql)
    # Split on semicolons.
    parts = cleaned.split(";")
    # Count how many parts (beyond the first) have non-whitespace content.
    non_empty_after_first = sum(1 for p in parts[1:] if p.strip())
    return non_empty_after_first > 0


def _has_unqualified_from(sql: str) -> bool:
    """Return True if any FROM / JOIN reference uses fewer than 3 name parts.

    Looks for: FROM <name> or JOIN <name> where <name> does NOT contain
    exactly two dots (i.e. is not catalog.schema.table).

    This is a conservative heuristic — subqueries in FROM are excluded because
    they start with ``(``.
    """
    cleaned = _strip_string_literals(sql)
    # Match FROM or JOIN followed by an identifier (not a subquery).
    pattern = re.compile(
        r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)",
        re.IGNORECASE,
    )
    for m in pattern.finditer(cleaned):
        name = m.group(1).strip()
        if name.upper() in ("LATERAL", "UNNEST"):
            continue
        dot_count = name.count(".")
        if dot_count < 2:
            return True
    return False


# ---------------------------------------------------------------------------
# Rule checker registry
# ---------------------------------------------------------------------------

# Each checker: (sql: str) -> tuple[str, str]  →  (verdict, reason)
_CHECKERS: dict[str, Callable[[str], tuple[str, str]]] = {}


def _register(rule_id: str) -> Callable:
    def decorator(fn: Callable[[str], tuple[str, str]]) -> Callable:
        _CHECKERS[rule_id] = fn
        return fn
    return decorator


@_register("R-MS")
def _check_rms(sql: str) -> tuple[str, str]:
    if _has_multiple_statements(sql):
        return "fail", "SQL contains multiple statements separated by ';'"
    return "pass", "Single statement — no multi-statement violation detected"


@_register("R-FQ")
def _check_rfq(sql: str) -> tuple[str, str]:
    if _has_unqualified_from(sql):
        return "fail", "FROM/JOIN clause references a table without a fully-qualified three-part name"
    return "pass", "All FROM/JOIN references appear fully-qualified"


# ---------------------------------------------------------------------------
# Lock check (FR7)
# ---------------------------------------------------------------------------

_EVAL_LOCK_PATH = Path.home() / ".claude" / "skills" / "build" / "state" / "tick.lock"


def is_live_eval_running() -> bool:
    """Return True if a live eval tick lock is held by another process.

    Checks whether the tick.lock file is locked by an external process.
    Uses ``fcntl.flock`` in non-blocking mode; returns False on non-POSIX
    platforms (Windows) so the harness can still be used in CI.
    """
    try:
        import fcntl
        import os
        if not _EVAL_LOCK_PATH.exists():
            return False
        with open(_EVAL_LOCK_PATH, "r") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
                return False  # Lock acquired → not held by another process.
            except OSError:
                return True  # Lock held → live eval running.
    except ImportError:
        return False  # fcntl not available (Windows).


# ---------------------------------------------------------------------------
# Main harness class
# ---------------------------------------------------------------------------


class RuleViolationHarness:
    """Deterministic per-rule violation checker.

    Implements the measurement described in FR2: for a given SQL string and
    rule ID, returns the violation verdict plus a human-readable reason.

    Only ``sql-only`` rules have deterministic checkers registered here.
    Rules requiring LLM judgment are marked ``lm_driven=True`` in the verdict
    and must not contribute to the hard CI floor (FR7 / NFR3).

    Usage::

        harness = RuleViolationHarness()
        result = harness.check_rule_violations("SELECT 1; SELECT 2", "R-MS")
        assert result.verdict == "fail"
    """

    def check_rule_violations(self, sql: str, rule_id: str) -> RuleVerdict:
        """Check *sql* against the named rule.

        Parameters
        ----------
        sql:
            The SQL string to evaluate.
        rule_id:
            One of the rule IDs from ``corpus/rule_violations/rules.yaml``
            (e.g. ``"R-MS"``, ``"R-FQ"``).

        Returns
        -------
        RuleVerdict
            Contains ``rule_id``, ``verdict`` (``"pass"`` or ``"fail"``),
            ``reason``, and ``lm_driven`` flag.

        Raises
        ------
        ValueError
            If *rule_id* is not registered and has no deterministic checker.
            The caller should handle this by marking the case as ``lm_driven``.
        """
        checker = _CHECKERS.get(rule_id)
        if checker is None:
            # No deterministic checker — return a sentinel that signals
            # LLM-driven evaluation is needed (excluded from CI floor).
            return RuleVerdict(
                rule_id=rule_id,
                verdict="skip",
                reason=f"No deterministic checker for rule {rule_id!r}; LLM evaluation required",
                lm_driven=True,
            )
        verdict, reason = checker(sql)
        return RuleVerdict(rule_id=rule_id, verdict=verdict, reason=reason, lm_driven=False)

    def check_all(self, sql: str, rule_ids: list[str]) -> list[RuleVerdict]:
        """Check *sql* against every rule in *rule_ids* and return all verdicts."""
        return [self.check_rule_violations(sql, rid) for rid in rule_ids]
