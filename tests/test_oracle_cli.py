"""Tests for oracle_cli — all mocked; no live mqo-pg-query binary required."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mqo_eval.oracle_cli import (
    CliOracleConfig,
    OracleError,
    Oversize,
    ReferenceTable,
    cli_precheck,
    execute_golden_cli,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
CORPUS = REPO_ROOT / "corpus" / "tpcds_sql_derived_limited.yaml"


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    """Build a fake CompletedProcess."""
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _cfg(**kwargs: object) -> CliOracleConfig:
    return CliOracleConfig(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. execute_golden_cli — columns/rows → ReferenceTable
# ---------------------------------------------------------------------------


def test_columns_rows_success() -> None:
    payload = json.dumps({"columns": ["a", "b"], "rows": [[1, "x"], [2, "y"]]})
    with patch("subprocess.run", return_value=_proc(stdout=payload)):
        result = execute_golden_cli(_cfg(), "c1", "SELECT a, b FROM t")

    assert isinstance(result, ReferenceTable)
    assert result.columns == ["a", "b"]
    assert result.rows == [[1, "x"], [2, "y"]]


def test_empty_table_is_valid_reference() -> None:
    payload = json.dumps({"columns": ["x"], "rows": []})
    with patch("subprocess.run", return_value=_proc(stdout=payload)):
        result = execute_golden_cli(_cfg(), "c2", "SELECT x FROM t WHERE 1=0")

    assert isinstance(result, ReferenceTable)
    assert result.columns == ["x"]
    assert result.rows == []


def test_cell_type_coercion() -> None:
    """None stays None, bool → int, int/float unchanged, str stripped."""
    payload = json.dumps(
        {"columns": ["n", "b", "i", "f", "s"],
         "rows": [[None, True, 42, 3.14, "  hello  "]]}
    )
    with patch("subprocess.run", return_value=_proc(stdout=payload)):
        result = execute_golden_cli(_cfg(), "c3", "SELECT * FROM t")

    assert isinstance(result, ReferenceTable)
    row = result.rows[0]
    assert row[0] is None
    assert row[1] == 1 and isinstance(row[1], int)   # True → 1
    assert row[2] == 42 and isinstance(row[2], int)
    assert abs(row[3] - 3.14) < 1e-9 and isinstance(row[3], float)
    assert row[4] == "hello" and isinstance(row[4], str)  # stripped


# ---------------------------------------------------------------------------
# 2. execute_golden_cli — oversize → Oversize
# ---------------------------------------------------------------------------


def test_oversize_result() -> None:
    payload = json.dumps({"oversize": {"observed_at_least": 60001, "cap": 60000}})
    with patch("subprocess.run", return_value=_proc(stdout=payload)):
        result = execute_golden_cli(_cfg(), "c4", "SELECT * FROM big_table")

    assert isinstance(result, Oversize)
    assert result.observed_at_least == 60001
    assert result.cap == 60000


# ---------------------------------------------------------------------------
# 3. execute_golden_cli — {"error":...} → OracleError
# ---------------------------------------------------------------------------


def test_error_json_result() -> None:
    payload = json.dumps({"error": {"message": "syntax error near X"}})
    with patch("subprocess.run", return_value=_proc(stdout=payload)):
        result = execute_golden_cli(_cfg(), "c5", "BAD SQL")

    assert isinstance(result, OracleError)
    assert result.case_id == "c5"
    assert "syntax error" in result.message


def test_error_nonzero_exit() -> None:
    with patch(
        "subprocess.run",
        return_value=_proc(stdout="", stderr="connection refused", returncode=1),
    ):
        result = execute_golden_cli(_cfg(), "c6", "SELECT 1")

    assert isinstance(result, OracleError)
    assert result.case_id == "c6"
    assert "connection refused" in result.message


# ---------------------------------------------------------------------------
# 4. execute_golden_cli — malformed / non-JSON → OracleError
# ---------------------------------------------------------------------------


def test_malformed_non_json() -> None:
    with patch("subprocess.run", return_value=_proc(stdout="not json at all")):
        result = execute_golden_cli(_cfg(), "c7", "SELECT 1")

    assert isinstance(result, OracleError)
    assert "not valid JSON" in result.message


def test_empty_stdout() -> None:
    with patch("subprocess.run", return_value=_proc(stdout="")):
        result = execute_golden_cli(_cfg(), "c8", "SELECT 1")

    assert isinstance(result, OracleError)
    assert "no output" in result.message


def test_unrecognised_json_schema() -> None:
    payload = json.dumps({"unexpected_key": 123})
    with patch("subprocess.run", return_value=_proc(stdout=payload)):
        result = execute_golden_cli(_cfg(), "c9", "SELECT 1")

    assert isinstance(result, OracleError)
    assert "unrecognised schema" in result.message


# ---------------------------------------------------------------------------
# 5. execute_golden_cli — timeout → OracleError, run continues
# ---------------------------------------------------------------------------


def test_timeout_returns_oracle_error() -> None:
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="mqo-pg-query", timeout=120)):
        result = execute_golden_cli(_cfg(timeout_s=120), "c10", "SELECT 1")

    assert isinstance(result, OracleError)
    assert "timed out" in result.message
    assert result.case_id == "c10"


# ---------------------------------------------------------------------------
# 6. execute_golden_cli — binary not found → OracleError
# ---------------------------------------------------------------------------


def test_binary_not_found_returns_oracle_error() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
        result = execute_golden_cli(_cfg(gold_query_cmd="nonexistent-binary"), "c11", "SELECT 1")

    assert isinstance(result, OracleError)
    assert "not found" in result.message


# ---------------------------------------------------------------------------
# 7. cli_precheck — success path
# ---------------------------------------------------------------------------


def test_precheck_success() -> None:
    payload = json.dumps({"columns": ["?column?"], "rows": [[1]]})
    with patch("subprocess.run", return_value=_proc(stdout=payload)):
        cli_precheck(_cfg())  # should not raise


# ---------------------------------------------------------------------------
# 8. cli_precheck — binary missing names the binary and suggests PRD
# ---------------------------------------------------------------------------


def test_precheck_binary_not_found() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError()), pytest.raises(RuntimeError, match="binary not found"):
        cli_precheck(_cfg(gold_query_cmd="missing-binary"))


def test_precheck_binary_not_found_suggests_prd() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError()), pytest.raises(RuntimeError, match="PRD-mqo-pgwire-query-cli"):
        cli_precheck(_cfg())


# ---------------------------------------------------------------------------
# 9. cli_precheck — non-zero exit → RuntimeError with stderr line
# ---------------------------------------------------------------------------


def test_precheck_nonzero_exit() -> None:
    with (
        patch(
            "subprocess.run",
            return_value=_proc(stdout="", stderr="OIDC token fetch failed", returncode=2),
        ),
        pytest.raises(RuntimeError, match="OIDC token fetch failed"),
    ):
        cli_precheck(_cfg())


# ---------------------------------------------------------------------------
# 10. cli_precheck — timeout → RuntimeError
# ---------------------------------------------------------------------------


def test_precheck_timeout() -> None:
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="mqo-pg-query", timeout=9)), pytest.raises(RuntimeError, match="timed out"):
        cli_precheck(_cfg())


# ---------------------------------------------------------------------------
# 11. cli_precheck — non-JSON stdout → RuntimeError
# ---------------------------------------------------------------------------


def test_precheck_non_json_stdout() -> None:
    with patch("subprocess.run", return_value=_proc(stdout="garbage output")), pytest.raises(RuntimeError, match="not valid JSON"):
        cli_precheck(_cfg())


# ---------------------------------------------------------------------------
# 12. placeholder substitution flows through to subprocess command
# ---------------------------------------------------------------------------


def test_placeholder_substitution_in_command() -> None:
    """CatalogName/ModelName are substituted before passing sql to subprocess."""
    payload = json.dumps({"columns": ["x"], "rows": []})
    captured_calls: list[list[str]] = []

    def _capture(cmd: list[str], **kwargs: object) -> MagicMock:  # type: ignore[return]
        captured_calls.append(cmd)
        return _proc(stdout=payload)

    with patch("subprocess.run", side_effect=_capture):
        execute_golden_cli(
            _cfg(catalog_name="my_cat", model_name="my_model"),
            "c12",
            "SELECT * FROM ${CatalogName}.${ModelName}.sales",
        )

    assert captured_calls
    sql_arg_idx = captured_calls[0].index("--sql") + 1
    sql_passed = captured_calls[0][sql_arg_idx]
    assert "my_cat" in sql_passed
    assert "my_model" in sql_passed
    assert "${CatalogName}" not in sql_passed
    assert "${ModelName}" not in sql_passed


# ---------------------------------------------------------------------------
# 13. --endpoint forwarded to subprocess command when set
# ---------------------------------------------------------------------------


def test_endpoint_forwarded_when_set() -> None:
    payload = json.dumps({"columns": ["x"], "rows": []})
    captured_calls: list[list[str]] = []

    def _capture(cmd: list[str], **kwargs: object) -> MagicMock:  # type: ignore[return]
        captured_calls.append(cmd)
        return _proc(stdout=payload)

    with patch("subprocess.run", side_effect=_capture):
        execute_golden_cli(
            _cfg(endpoint="atscale.example.com:15432"),
            "c13",
            "SELECT 1",
        )

    assert "--endpoint" in captured_calls[0]
    ep_idx = captured_calls[0].index("--endpoint") + 1
    assert captured_calls[0][ep_idx] == "atscale.example.com:15432"


def test_endpoint_not_forwarded_when_empty() -> None:
    payload = json.dumps({"columns": ["x"], "rows": []})
    captured_calls: list[list[str]] = []

    def _capture(cmd: list[str], **kwargs: object) -> MagicMock:  # type: ignore[return]
        captured_calls.append(cmd)
        return _proc(stdout=payload)

    with patch("subprocess.run", side_effect=_capture):
        execute_golden_cli(_cfg(endpoint=""), "c14", "SELECT 1")

    assert "--endpoint" not in captured_calls[0]


# ---------------------------------------------------------------------------
# 14. --oracle cli is accepted and dispatches to oracle_cli
# ---------------------------------------------------------------------------


def test_cli_oracle_accepted_by_cli_parser() -> None:
    """--oracle cli must be a valid choice in the argument parser."""
    from mqo_eval.cli import _build_parser

    p = _build_parser()
    sub = p._subparsers._actions[1].choices["run"]  # type: ignore[attr-defined]
    help_text = sub.format_help()
    assert "cli" in help_text
    assert "--gold-query-cmd" in help_text
    assert "--cli-endpoint" in help_text


def test_cli_oracle_dispatches_to_execute_golden_cli(tmp_path: Path) -> None:
    """When --oracle cli, runner calls execute_golden_cli (not execute_golden)."""
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus

    ref = ReferenceTable(columns=["x"], rows=[[1]])
    agent_out = json.dumps({"answer_type": "tabular", "columns": ["x"], "rows": [[1]]})

    with (
        patch("mqo_eval.runner.cli_precheck"),
        patch("mqo_eval.runner.execute_golden_cli", return_value=ref) as mock_cli,
        patch("mqo_eval.runner.execute_golden") as mock_pgwire,
        patch("mqo_eval.runner._invoke_agent", return_value=agent_out),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        run_corpus(
            corpus,
            agent,
            "m",
            "cli",
            tmp_path / "r",
            {
                "oracle": "cli",
                "gold_query_cmd": "mqo-pg-query",
                "cli_endpoint": "",
                "catalog_name": "cat",
                "model_name": "mod",
                "pass_threshold": 0.95,
                "repeat": 1,
            },
        )

    assert mock_cli.call_count > 0
    mock_pgwire.assert_not_called()


# ---------------------------------------------------------------------------
# 15. cli precheck fail-fast: no agent invocations
# ---------------------------------------------------------------------------


def test_cli_precheck_fail_fast(tmp_path: Path) -> None:
    """If cli_precheck raises, run_corpus propagates the error before any agent call."""
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus

    with (
        patch(
            "mqo_eval.runner.cli_precheck",
            side_effect=RuntimeError("binary not found: mqo-pg-query"),
        ),
        patch("mqo_eval.runner._invoke_agent") as mock_agent,
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="test", command="ignored")
        with pytest.raises(RuntimeError, match="binary not found"):
            run_corpus(
                corpus,
                agent,
                "m",
                "cli",
                tmp_path / "r",
                {
                    "oracle": "cli",
                    "gold_query_cmd": "missing",
                    "cli_endpoint": "",
                    "catalog_name": "c",
                    "model_name": "m",
                    "pass_threshold": 0.95,
                    "repeat": 1,
                },
            )
    mock_agent.assert_not_called()
