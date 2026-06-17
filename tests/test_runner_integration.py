"""Integration tests for the wired oracle+scoring runner (all mocked)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CORPUS = Path(__file__).parent.parent / "corpus" / "tpcds_sql_derived_limited.yaml"
REPO_ROOT = Path(__file__).parent.parent


def _make_ref_table(cols: list[str], rows: list[list]) -> object:
    from mqo_eval.oracle_pgwire import ReferenceTable
    return ReferenceTable(columns=cols, rows=rows)


def _make_oversize() -> object:
    from mqo_eval.oracle_pgwire import Oversize
    return Oversize(observed_at_least=60_000, cap=50_000)


def _make_oracle_error(case_id: str) -> object:
    from mqo_eval.oracle_pgwire import OracleError
    return OracleError(case_id=case_id, message="sql error")


def _tabular_answer(cols: list[str], rows: list[list]) -> str:
    return json.dumps({"answer_type": "tabular", "columns": cols, "rows": rows})


def _decline_answer(reason: str = "no path") -> str:
    return json.dumps({"answer_type": "cannot_answer", "reason": reason})


# 1 — fixture mode still works offline
def test_fixture_mode_offline(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    corpus = load_corpus(CORPUS)
    agent = AgentEntry(name="stub", command="python -m mqo_eval.stub_agent")
    record, dest = run_corpus(
        corpus, agent, "m", "fixture", tmp_path / "r",
        {"oracle": "fixture", "repeat": 1},
    )
    assert dest.exists()
    assert record.summary.active == 20


# 2 — pgwire: correct answer scores correct
def test_pgwire_mode_scores_correct(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    ref = _make_ref_table(["col1"], [[42]])
    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ref),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["col1"], [[42]])),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        record, _ = run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1},
        )
    correct = [c for c in record.cases if c.verdict == "correct"]
    assert len(correct) > 0
    assert all(c.row_recall is not None for c in correct)


# 3 — pgwire: wrong answer scores wrong with recall
def test_pgwire_mode_wrong_answer(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    ref = _make_ref_table(["col1"], [[1], [2], [3]])
    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ref),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["col1"], [[9], [9], [9]])),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        record, _ = run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1},
        )
    wrong = [c for c in record.cases if c.verdict == "wrong"]
    assert len(wrong) > 0


# 4 — pgwire precheck fail-fast: no agent invocations
def test_pgwire_precheck_fail_fast(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    with (
        patch("mqo_eval.runner.pgwire_precheck",
              side_effect=RuntimeError("cannot connect")),
        patch("mqo_eval.runner._invoke_agent") as mock_agent,
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        with pytest.raises(RuntimeError, match="cannot connect"):
            run_corpus(
                corpus, agent, "m", "pgwire", tmp_path / "r",
                {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
                 "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1},
            )
    mock_agent.assert_not_called()


# 5 — oracle error for one case: that case = no_bind, run continues
def test_oracle_error_continues(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    err = _make_oracle_error("q1")
    ref = _make_ref_table(["x"], [[1]])
    call_count = 0

    def side_effect(cfg, case_id, sql):
        nonlocal call_count
        call_count += 1
        return err if call_count == 1 else ref

    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", side_effect=side_effect),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["x"], [[1]])),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        record, _ = run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1},
        )
    no_bind = [c for c in record.cases if c.verdict == "no_bind"]
    assert len(no_bind) >= 1  # the oracle-error case
    # run should have continued (more cases processed)
    assert len(record.cases) == 21  # 20 active + 1 skipped


# 6 — oversize reference: verdict = oversize
def test_oversize_reference(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    ovs = _make_oversize()
    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ovs),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["x"], [[1]])),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        record, _ = run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1},
        )
    oversize = [c for c in record.cases if c.verdict == "oversize"]
    assert len(oversize) > 0


# 7 — k-of-n: 3/4 correct → overall correct
def test_k_of_n_correct(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    ref = _make_ref_table(["x"], [[1]])
    verdicts_seq = iter(["correct", "correct", "correct", "wrong"] * 21)

    from mqo_eval.scoring import ScoringResult

    def mock_score(*args, **kwargs):
        v = next(verdicts_seq)
        return ScoringResult(verdict=v)

    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ref),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["x"], [[1]])),
        patch("mqo_eval.runner.score_case", side_effect=mock_score),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        record, _ = run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95,
             "repeat": 4, "min_pass_reps": 3},
        )
    active = [c for c in record.cases if c.verdict != "skipped"]
    assert all(c.verdict == "correct" for c in active)
    assert all(c.rep_verdicts is not None and len(c.rep_verdicts) == 4 for c in active)


# 8 — k-of-n: 2/4 correct → overall wrong
def test_k_of_n_wrong(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    from mqo_eval.scoring import ScoringResult
    ref = _make_ref_table(["x"], [[1]])
    verdicts_seq = iter(["correct", "correct", "wrong", "wrong"] * 21)

    def mock_score(*args, **kwargs):
        return ScoringResult(verdict=next(verdicts_seq))

    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ref),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["x"], [[1]])),
        patch("mqo_eval.runner.score_case", side_effect=mock_score),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        record, _ = run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95,
             "repeat": 4, "min_pass_reps": 3},
        )
    active = [c for c in record.cases if c.verdict != "skipped"]
    assert all(c.verdict == "wrong" for c in active)


# 9 — pass-threshold boundary
def test_pass_threshold_boundary(tmp_path: Path) -> None:
    from mqo_eval.oracle_pgwire import ReferenceTable
    from mqo_eval.scoring import score_case
    from mqo_eval.contract import TabularAnswer
    ref = ReferenceTable(columns=["x"], rows=[[i] for i in range(100)])
    # 96 of 100 rows match → recall 0.96
    cand_rows = [[i] for i in range(96)] + [[999], [999], [999], [999]]
    cand = TabularAnswer(columns=["x"], rows=cand_rows)
    result_high = score_case(ref, cand, pass_threshold=0.95)
    result_low = score_case(ref, cand, pass_threshold=0.97)
    assert result_high.verdict == "correct"
    assert result_low.verdict == "wrong"


# 10 — equivalent_attributes passed through to score_case
def test_equivalent_attributes_passed_through(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    ref = _make_ref_table(["col_a"], [[1]])
    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ref),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["col_b"], [[1]])),
        patch("mqo_eval.runner.score_case") as mock_score,
    ):
        from mqo_eval.scoring import ScoringResult
        mock_score.return_value = ScoringResult(verdict="correct")
        # inject a corpus case with equivalent_attributes
        corpus = load_corpus(CORPUS)
        corpus.queries[0].equivalent_attributes = [["col_a", "col_b"]]
        agent = AgentEntry(name="test", command="ignored")
        run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1},
        )
    # score_case should be called with the equiv groups
    calls = mock_score.call_args_list
    assert any(
        call.kwargs.get("equiv") == [["col_a", "col_b"]] or
        (len(call.args) >= 4 and call.args[3] == [["col_a", "col_b"]])
        for call in calls
    )


# 11 — CLI flags present
def test_cli_flags_present() -> None:
    from mqo_eval.cli import _build_parser
    p = _build_parser()
    import sys
    import io
    buf = io.StringIO()
    try:
        p.parse_args(["run", "--help"])
    except SystemExit:
        pass
    # Parse just the run subparser's help
    sub = p._subparsers._actions[1].choices["run"]
    opts = sub.format_help()
    assert "--pass-threshold" in opts
    assert "--repeat" in opts
    assert "--min-pass-reps" in opts
    assert "--oracle" in opts


# 12 — mean_row_recall in summary
def test_mean_recall_in_summary(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    from mqo_eval.scoring import ScoringResult, TableMetrics

    ref = _make_ref_table(["x"], [[1]])
    m = TableMetrics(row_recall=0.9, row_jaccard=0.85, column_recall=1.0, column_jaccard=1.0)

    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ref),
        patch("mqo_eval.runner._invoke_agent",
              return_value=_tabular_answer(["x"], [[1]])),
        patch("mqo_eval.runner.score_case",
              return_value=ScoringResult(verdict="correct", metrics=m)),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        record, _ = run_corpus(
            corpus, agent, "m", "pgwire", tmp_path / "r",
            {"oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
             "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1},
        )
    assert record.summary.mean_row_recall is not None
    assert abs(record.summary.mean_row_recall - 0.9) < 1e-6
