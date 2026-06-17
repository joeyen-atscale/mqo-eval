"""Tests for mqo_eval.scoring — all mocked, no live DB."""
from __future__ import annotations

from mqo_eval.contract import (
    CannotAnswer,
    HandleAnswer,
    ScalarAnswer,
    TabularAnswer,
)
from mqo_eval.oracle_pgwire import OracleError, Oversize, ReferenceTable
from mqo_eval.scoring import compute_metrics, score_case

# ── Helpers ───────────────────────────────────────────────────────────────────


def ref(cols: list[str], rows: list[list]) -> ReferenceTable:
    return ReferenceTable(columns=cols, rows=rows)


def tabular(cols: list[str], rows: list[list]) -> TabularAnswer:
    return TabularAnswer(columns=cols, rows=rows)


# ── Test 1: empty reference + decline → correct ───────────────────────────────


def test_empty_reference_passes_on_decline() -> None:
    reference = ref(["col_a"], [])
    candidate = CannotAnswer(reason="cannot answer")
    result = score_case(reference, candidate)
    assert result.verdict == "correct"


# ── Test 2: empty reference + rows → wrong ────────────────────────────────────


def test_empty_reference_fails_on_rows() -> None:
    reference = ref(["col_a"], [])
    candidate = tabular(["col_a"], [["x"], ["y"]])
    result = score_case(reference, candidate)
    assert result.verdict == "wrong"
    assert "2" in (result.detail or "")


# ── Test 3: row recall formula ────────────────────────────────────────────────


def test_row_recall_formula() -> None:
    # Reference has 3 rows; candidate matches 2 of them
    reference = ref(["a"], [[1], [2], [3]])
    cand_table = ReferenceTable(columns=["a"], rows=[[1], [2]])
    metrics = compute_metrics(reference, cand_table)
    # recall = 2/3, jaccard = 2/(3+2-2) = 2/3
    assert abs(metrics.row_recall - 2 / 3) < 1e-6
    assert abs(metrics.row_jaccard - 2 / 3) < 1e-6


# ── Test 4: pass threshold correct ────────────────────────────────────────────


def test_pass_threshold_correct() -> None:
    # 24/25 = 0.96 recall → passes at threshold 0.95
    rows = [[i] for i in range(25)]
    reference = ref(["n"], rows)
    candidate = tabular(["n"], rows[:24])
    result = score_case(reference, candidate, pass_threshold=0.95)
    assert result.verdict == "correct"
    assert result.metrics is not None
    assert result.metrics.row_recall >= 0.95


# ── Test 5: pass threshold wrong ──────────────────────────────────────────────


def test_pass_threshold_wrong() -> None:
    # 23/25 = 0.92 recall → fails at threshold 0.95
    rows = [[i] for i in range(25)]
    reference = ref(["n"], rows)
    candidate = tabular(["n"], rows[:23])
    result = score_case(reference, candidate, pass_threshold=0.95)
    assert result.verdict == "wrong"
    assert result.metrics is not None
    assert result.metrics.row_recall < 0.95


# ── Test 6: Jaccard never gates — superset with recall=1.0 passes per FR5 ─────


def test_jaccard_never_gates() -> None:
    # Superset: candidate has all reference rows PLUS extra rows.
    # FR5: correct iff column_recall==1.0 AND row_recall >= threshold.
    # NG3: Jaccard reported only, never flips verdict.
    # A row-superset has row_recall=1.0 → passes; Jaccard < 1 is reported only.
    reference = ref(["x"], [[1], [2], [3]])
    candidate = tabular(["x"], [[1], [2], [3], [4], [5]])  # superset
    result = score_case(reference, candidate, pass_threshold=0.95)
    # jaccard = 3/(3+5-3) = 0.6 (reported but does not gate)
    assert result.verdict == "correct"
    assert result.metrics is not None
    assert result.metrics.row_recall == 1.0
    assert result.metrics.row_jaccard < 1.0  # jaccard < 1 but verdict still correct


# ── Test 7: missing reference column → wrong ──────────────────────────────────


def test_column_recall_gates() -> None:
    # Reference has columns [a, b], candidate only has [a] → column_recall = 0.5
    reference = ref(["a", "b"], [[1, 2], [3, 4]])
    candidate = tabular(["a"], [[1], [3]])
    result = score_case(reference, candidate, pass_threshold=0.95)
    assert result.verdict == "wrong"
    assert result.metrics is not None
    assert result.metrics.column_recall < 1.0


# ── Test 8: scalar 1x1 correct ────────────────────────────────────────────────


def test_scalar_1x1_correct() -> None:
    reference = ref(["total"], [[42]])
    candidate = ScalarAnswer(value=42)
    result = score_case(reference, candidate)
    assert result.verdict == "correct"


# ── Test 9: numeric tolerance ─────────────────────────────────────────────────


def test_numeric_tolerance() -> None:
    # 1.0001 vs 1.0000 — within rel_tol=1e-3
    reference = ref(["val"], [[1.0000]])
    candidate = ScalarAnswer(value=1.0001)
    result = score_case(reference, candidate)
    assert result.verdict == "correct"


# ── Test 10: oversize reference ───────────────────────────────────────────────


def test_oversize_reference() -> None:
    reference = Oversize(observed_at_least=50001, cap=50000)
    candidate = tabular(["a"], [[1]])
    result = score_case(reference, candidate)
    assert result.verdict == "oversize"
    assert "50000" in (result.detail or "")


# ── Test 11: equivalent attributes ───────────────────────────────────────────


def test_equivalent_attributes() -> None:
    # Reference uses column "store_sales_net_profit", candidate uses "net_profit"
    # They are in the same equiv group → should match
    reference = ref(["store_sales_net_profit"], [[100.0], [200.0]])
    candidate = tabular(["net_profit"], [[100.0], [200.0]])
    equiv = [["store_sales_net_profit", "net_profit"]]
    result = score_case(reference, candidate, pass_threshold=0.95, equiv=equiv)
    assert result.verdict == "correct"
    assert result.metrics is not None
    assert result.metrics.column_recall == 1.0


# ── Test 12: handle answer returns no_bind ────────────────────────────────────


def test_handle_answer_returns_no_bind() -> None:
    reference = ref(["col_a"], [[1], [2]])
    candidate = HandleAnswer(handle_id="abc-123")
    result = score_case(reference, candidate)
    assert result.verdict == "no_bind"
    assert "abc-123" in (result.detail or "")


# ── Additional edge case tests ────────────────────────────────────────────────


def test_oracle_error_returns_no_bind() -> None:
    oracle_err = OracleError(case_id="test-1", message="connection refused")
    candidate = tabular(["a"], [[1]])
    result = score_case(oracle_err, candidate)
    assert result.verdict == "no_bind"


def test_none_reference_returns_no_bind() -> None:
    candidate = tabular(["a"], [[1]])
    result = score_case(None, candidate)
    assert result.verdict == "no_bind"


def test_cannot_answer_on_nonempty_reference_is_wrong() -> None:
    reference = ref(["a"], [[1], [2]])
    candidate = CannotAnswer(reason="path incompatible")
    result = score_case(reference, candidate)
    assert result.verdict == "wrong"


def test_scalar_on_multirow_reference_is_wrong() -> None:
    reference = ref(["a"], [[1], [2], [3]])
    candidate = ScalarAnswer(value=42)
    result = score_case(reference, candidate)
    assert result.verdict == "wrong"
    assert "multi-row" in (result.detail or "")


def test_compute_metrics_exact_match() -> None:
    reference = ref(["a", "b"], [[1, "x"], [2, "y"]])
    cand_table = ReferenceTable(columns=["a", "b"], rows=[[1, "x"], [2, "y"]])
    metrics = compute_metrics(reference, cand_table)
    assert metrics.row_recall == 1.0
    assert metrics.row_jaccard == 1.0
    assert metrics.column_recall == 1.0
    assert metrics.column_jaccard == 1.0


def test_empty_candidate_on_nonempty_reference() -> None:
    reference = ref(["a"], [[1], [2]])
    candidate = tabular(["a"], [])
    result = score_case(reference, candidate, pass_threshold=0.95)
    assert result.verdict == "wrong"
    assert result.metrics is not None
    assert result.metrics.row_recall == 0.0
