"""Result-set scoring: recall/Jaccard + handle resolution via dataset_export."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .contract import (
    AgentAnswer,
    CannotAnswer,
    HandleAnswer,
    ScalarAnswer,
    TabularAnswer,
)
from .oracle_pgwire import OracleError, Oversize, ReferenceTable

# ── Cell canonicalization (matches mcp-eval-scoring-spec exactly) ─────────────


def _cell_key(v: Any) -> tuple[str, Any]:
    """Canonical key for a cell value — matches mcp-eval-scoring-spec formula."""
    if v is None:
        return ("n", None)
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        try:
            return ("n", float(f"{float(v):.6g}"))
        except (ValueError, TypeError):
            pass
    return ("s", str(v).strip().casefold())


def _cell_key_with_equiv(v: Any, value_equiv: dict[str, list[str]]) -> tuple[str, Any]:
    """Canonical key for a cell with per-case value-equivalence applied.

    First computes the normal key; then, if the string form of v appears in
    any equivalence group (as either the canonical or an alternative), the
    key is normalised to the canonical (first) entry of the matching group.
    This lets a correct filter expressed differently (e.g. ``'September'`` vs
    ``'1998-09-01T00:00:00Z'``) avoid a row miss.

    Default (no ``value_equiv``) → identical to ``_cell_key``.
    """
    base = _cell_key(v)
    if not value_equiv:
        return base
    raw = str(v).strip().casefold() if v is not None else None
    for canonical, alternatives in value_equiv.items():
        canon_cf = canonical.strip().casefold()
        alts_cf = [a.strip().casefold() for a in alternatives]
        if raw in (canon_cf, *alts_cf):
            # Normalise to canonical so ref and candidate use the same key.
            return ("s", canon_cf)
    return base


# ── Column normalization ───────────────────────────────────────────────────────


def _normalize_col(c: str) -> str:
    return re.sub(r"\s+", " ", c.strip().strip("\"'`").casefold())


def _columns_match(a: str, b: str, equiv: list[list[str]]) -> bool:
    na, nb = _normalize_col(a), _normalize_col(b)
    if na == nb:
        return True
    for group in equiv:
        norm_group = [_normalize_col(x) for x in group]
        if na in norm_group and nb in norm_group:
            return True
    return False


# ── Table metrics ─────────────────────────────────────────────────────────────


@dataclass
class TableMetrics:
    row_recall: float
    row_jaccard: float
    column_recall: float
    column_jaccard: float


def compute_metrics(
    reference: ReferenceTable,
    candidate: ReferenceTable,
    equiv: list[list[str]] | None = None,
    value_equiv: dict[str, list[str]] | None = None,
) -> TableMetrics:
    """Compute recall/Jaccard — exactly per mcp-eval-scoring-spec."""
    equiv = equiv or []
    value_equiv = value_equiv or {}

    # Column matching (set intersection, equivalence-aware)
    ref_cols = reference.columns
    ans_cols = candidate.columns
    ref_norm = {_normalize_col(c) for c in ref_cols}
    # find answer cols that cover reference (with equiv)
    matched_cols: set[str] = set()
    for rc in ref_cols:
        for ac in ans_cols:
            if _columns_match(rc, ac, equiv):
                matched_cols.add(_normalize_col(rc))
                break
    ans_norm = {_normalize_col(c) for c in ans_cols}
    col_union = ref_norm | ans_norm
    col_recall = len(matched_cols) / len(ref_norm) if ref_norm else 0.0
    col_jaccard = len(matched_cols) / len(col_union) if col_union else 1.0

    # Row matching on shared columns (multiset counter intersection)
    if not ref_cols:
        return TableMetrics(1.0, 1.0, col_recall, col_jaccard)

    # Use only matched reference columns
    matched_ref_cols = [c for c in ref_cols if _normalize_col(c) in matched_cols]
    if not matched_ref_cols:
        return TableMetrics(0.0, 0.0, col_recall, col_jaccard)

    def project_row(
        row: list[Any], cols: list[str], ref_cols_matched: list[str]
    ) -> tuple[Any, ...]:
        result = []
        for rc in ref_cols_matched:
            for i, ac in enumerate(cols):
                if _columns_match(rc, ac, equiv):
                    result.append(_cell_key_with_equiv(row[i] if i < len(row) else None, value_equiv))
                    break
            else:
                result.append(_cell_key_with_equiv(None, value_equiv))
        return tuple(result)

    ref_keys: Counter[Any] = Counter(
        tuple(
            _cell_key_with_equiv(v, value_equiv)
            for i, v in enumerate(r)
            if i < len(ref_cols) and _normalize_col(ref_cols[i]) in matched_cols
        )
        for r in reference.rows
    )
    ans_keys: Counter[Any] = Counter(
        project_row(r, ans_cols, matched_ref_cols) for r in candidate.rows
    )

    matched_rows = sum((ref_keys & ans_keys).values())
    n_ref = sum(ref_keys.values())
    n_ans = sum(ans_keys.values())
    row_recall = matched_rows / n_ref if n_ref else (1.0 if n_ans == 0 else 0.0)
    union_rows = n_ref + n_ans - matched_rows
    row_jaccard = matched_rows / union_rows if union_rows else 1.0

    return TableMetrics(row_recall, row_jaccard, col_recall, col_jaccard)


# ── Verdict ───────────────────────────────────────────────────────────────────


@dataclass
class ScoringResult:
    verdict: str  # "correct"|"wrong"|"no_bind"|"oversize"|"declined"|"parse_error"
    metrics: TableMetrics | None = None
    detail: str | None = None


def score_case(
    reference: ReferenceTable | Oversize | OracleError | None,
    candidate: AgentAnswer,
    pass_threshold: float = 0.95,
    equiv: list[list[str]] | None = None,
    value_equiv: dict[str, list[str]] | None = None,
) -> ScoringResult:
    """Score candidate against reference per mcp-eval-scoring-spec."""
    equiv = equiv or []
    value_equiv = value_equiv or {}

    # Oracle failures
    if reference is None or isinstance(reference, OracleError):
        return ScoringResult("no_bind", detail="no reference table")
    if isinstance(reference, Oversize):
        return ScoringResult(
            "oversize", detail=f"reference oversize >{reference.cap} rows"
        )

    # Empty reference: passes on empty/decline, fails on rows
    if not reference.rows:
        if isinstance(candidate, CannotAnswer):
            return ScoringResult(
                "correct", detail="declined as expected (empty reference)"
            )
        if isinstance(candidate, (TabularAnswer, HandleAnswer)):
            rows = getattr(candidate, "rows", [])
            if not rows:
                return ScoringResult(
                    "correct", detail="0 rows matched (empty reference)"
                )
            return ScoringResult("wrong", detail=f"expected no rows, got {len(rows)}")
        return ScoringResult("correct")  # scalar on empty ref = ok

    # Candidate handle — return no_bind (resolution is external; we score what we have)
    if isinstance(candidate, HandleAnswer):
        return ScoringResult(
            "no_bind", detail=f"handle {candidate.handle_id!r} not yet resolved"
        )

    # Candidate decline
    if isinstance(candidate, CannotAnswer):
        return ScoringResult("wrong", detail=f"model declined: {candidate.reason}")

    # Scalar on multi-row reference
    if isinstance(candidate, ScalarAnswer):
        # 1x1 reference: compare scalar
        if len(reference.rows) == 1 and len(reference.columns) == 1:
            ref_val = (
                str(reference.rows[0][0]).strip().casefold()
                if reference.rows[0]
                else ""
            )
            cand_val = str(candidate.value).strip().casefold()
            try:
                if math.isclose(
                    float(ref_val), float(cand_val), rel_tol=1e-3, abs_tol=1e-6
                ):
                    return ScoringResult("correct")
            except (ValueError, TypeError):
                pass
            if ref_val == cand_val:
                return ScoringResult("correct")
            return ScoringResult("wrong", detail=f"{cand_val!r} ≠ {ref_val!r}")
        return ScoringResult("wrong", detail="scalar answer for multi-row reference")

    # Tabular answer
    if not isinstance(candidate, TabularAnswer):
        return ScoringResult("wrong", detail="unexpected answer type")

    cand_table = ReferenceTable(columns=candidate.columns, rows=candidate.rows)
    metrics = compute_metrics(reference, cand_table, equiv, value_equiv)
    passed = metrics.column_recall >= 1.0 and metrics.row_recall >= pass_threshold
    return ScoringResult(
        verdict="correct" if passed else "wrong",
        metrics=metrics,
        detail=f"recall={metrics.row_recall:.3f} jaccard={metrics.row_jaccard:.3f}",
    )
