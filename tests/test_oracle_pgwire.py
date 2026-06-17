"""Tests for oracle_pgwire — all mocked, no live DB required."""

from __future__ import annotations

import decimal
from unittest.mock import MagicMock, patch

import pytest

from mqo_eval.oracle_pgwire import (
    OracleError,
    Oversize,
    PgwireConfig,
    ReferenceTable,
    execute_golden,
    pgwire_precheck,
    substitute_placeholders,
)

# ---------------------------------------------------------------------------
# 1. substitute_placeholders
# ---------------------------------------------------------------------------


def test_substitute_placeholders_both() -> None:
    sql = "SELECT * FROM ${CatalogName}.${ModelName}.sales"
    result = substitute_placeholders(sql, catalog="my_catalog", model="my_model")
    assert result == "SELECT * FROM my_catalog.my_model.sales"


def test_substitute_placeholders_catalog_only() -> None:
    sql = "FROM ${CatalogName}.schema"
    result = substitute_placeholders(sql, catalog="cat", model="mod")
    assert "${CatalogName}" not in result
    assert "cat" in result


def test_substitute_placeholders_model_only() -> None:
    sql = "USE ${ModelName}"
    result = substitute_placeholders(sql, catalog="cat", model="mod")
    assert result == "USE mod"


def test_substitute_placeholders_no_placeholders() -> None:
    sql = "SELECT 1"
    assert substitute_placeholders(sql, catalog="c", model="m") == "SELECT 1"


def test_substitute_placeholders_multiple_occurrences() -> None:
    sql = "${CatalogName}.${ModelName}.a JOIN ${CatalogName}.${ModelName}.b"
    result = substitute_placeholders(sql, catalog="C", model="M")
    assert result == "C.M.a JOIN C.M.b"


# ---------------------------------------------------------------------------
# 2. test_empty_result_is_valid_table
# ---------------------------------------------------------------------------


def test_empty_result_is_valid_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """0-row result → ReferenceTable with empty rows (not an error)."""
    monkeypatch.setenv("ATSCALE_PG_PASS", "secret")

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.description = [("col_a",), ("col_b",)]
    mock_cursor.fetchmany.return_value = []

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("psycopg2.connect", return_value=mock_conn):
        result = execute_golden(
            PgwireConfig(pg_host="fake-host"),
            case_id="case-001",
            expected_sql="SELECT col_a, col_b FROM t",
        )

    assert isinstance(result, ReferenceTable)
    assert result.columns == ["col_a", "col_b"]
    assert result.rows == []


# ---------------------------------------------------------------------------
# 3. test_oversize_cap_enforced
# ---------------------------------------------------------------------------


def test_oversize_cap_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetching cap+1 rows → Oversize (never a table)."""
    monkeypatch.setenv("ATSCALE_PG_PASS", "secret")

    cap = 10
    cfg = PgwireConfig(pg_host="fake-host", max_result_rows=cap)
    fetch_limit = cap + 1

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.description = [("id",)]
    # Return exactly fetch_limit rows to trigger oversize
    mock_cursor.fetchmany.return_value = [[i] for i in range(fetch_limit)]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("psycopg2.connect", return_value=mock_conn):
        result = execute_golden(
            cfg, case_id="case-002", expected_sql="SELECT id FROM t"
        )

    assert isinstance(result, Oversize)
    assert result.observed_at_least == fetch_limit
    assert result.cap == cap


# ---------------------------------------------------------------------------
# 4. test_hard_ceil_clamp
# ---------------------------------------------------------------------------


def test_hard_ceil_clamp() -> None:
    """max_result_rows > HARD_ROW_CEIL is silently clamped."""
    cfg = PgwireConfig(pg_host="h", max_result_rows=300_000)
    assert cfg.max_result_rows == PgwireConfig.HARD_ROW_CEIL
    assert cfg.max_result_rows == 200_000


def test_hard_ceil_boundary_exact() -> None:
    """max_result_rows == HARD_ROW_CEIL is kept as-is."""
    cfg = PgwireConfig(pg_host="h", max_result_rows=200_000)
    assert cfg.max_result_rows == 200_000


# ---------------------------------------------------------------------------
# 5. test_oracle_error_on_sql_failure
# ---------------------------------------------------------------------------


def test_oracle_error_on_sql_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A psycopg2 exception during execute → OracleError (run continues)."""
    monkeypatch.setenv("ATSCALE_PG_PASS", "secret")

    import psycopg2  # type: ignore[import]

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.execute.side_effect = psycopg2.ProgrammingError("syntax error near X")

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("psycopg2.connect", return_value=mock_conn):
        result = execute_golden(
            PgwireConfig(pg_host="fake-host"),
            case_id="bad-case",
            expected_sql="SELECT bad sql ;;",
        )

    assert isinstance(result, OracleError)
    assert result.case_id == "bad-case"
    assert "syntax error" in result.message


# ---------------------------------------------------------------------------
# 6. test_no_credential_in_error_message
# ---------------------------------------------------------------------------


def test_no_credential_in_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection failure error message must NOT contain the password."""
    secret_password = "super-secret-password-xyz"
    monkeypatch.setenv("ATSCALE_PG_PASS", secret_password)

    import psycopg2  # type: ignore[import]

    # The OperationalError message intentionally contains the password to confirm
    # our sanitisation strips it.
    leaky_message = f"could not connect: password={secret_password} wrong"
    with patch(
        "psycopg2.connect",
        side_effect=psycopg2.OperationalError(leaky_message),
    ), pytest.raises(RuntimeError) as exc_info:
        pgwire_precheck(PgwireConfig(pg_host="bad-host"))

    error_text = str(exc_info.value)
    assert secret_password not in error_text
    assert "bad-host" in error_text  # host:port present for diagnosis


def test_no_credential_in_oracle_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_golden OracleError message must NOT contain the password."""
    secret_password = "another-secret-9999"
    monkeypatch.setenv("ATSCALE_PG_PASS", secret_password)

    import psycopg2  # type: ignore[import]

    leaky_message = f"auth failed for user with password {secret_password}"
    with patch(
        "psycopg2.connect",
        side_effect=psycopg2.OperationalError(leaky_message),
    ):
        result = execute_golden(
            PgwireConfig(pg_host="bad-host"),
            case_id="case-pw",
            expected_sql="SELECT 1",
        )

    assert isinstance(result, OracleError)
    assert secret_password not in result.message


# ---------------------------------------------------------------------------
# 7. test_cell_typing
# ---------------------------------------------------------------------------


def test_cell_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cells are typed: int→int, float→float, None→None, text→str, Decimal→float."""
    monkeypatch.setenv("ATSCALE_PG_PASS", "secret")

    sample_row = [42, 3.14, None, "hello world  ", decimal.Decimal("1.5")]

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.description = [
        ("int_col",),
        ("float_col",),
        ("null_col",),
        ("text_col",),
        ("decimal_col",),
    ]
    mock_cursor.fetchmany.return_value = [sample_row]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("psycopg2.connect", return_value=mock_conn):
        result = execute_golden(
            PgwireConfig(pg_host="fake-host"),
            case_id="typing-case",
            expected_sql="SELECT * FROM t",
        )

    assert isinstance(result, ReferenceTable)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row[0] == 42 and isinstance(row[0], int)
    assert abs(row[1] - 3.14) < 1e-9 and isinstance(row[1], float)
    assert row[2] is None
    assert row[3] == "hello world" and isinstance(row[3], str)  # stripped
    assert abs(row[4] - 1.5) < 1e-9 and isinstance(row[4], float)


def test_cell_typing_trailing_whitespace_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """String cells have trailing/leading whitespace stripped."""
    monkeypatch.setenv("ATSCALE_PG_PASS", "secret")

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.description = [("name",)]
    mock_cursor.fetchmany.return_value = [["  padded  "]]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("psycopg2.connect", return_value=mock_conn):
        result = execute_golden(
            PgwireConfig(pg_host="fake-host"),
            case_id="ws-case",
            expected_sql="SELECT name FROM t",
        )

    assert isinstance(result, ReferenceTable)
    assert result.rows[0][0] == "padded"


# ---------------------------------------------------------------------------
# Bonus: trailing semicolons stripped from SQL
# ---------------------------------------------------------------------------


def test_trailing_semicolons_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQL with trailing ';' is stripped before execution."""
    monkeypatch.setenv("ATSCALE_PG_PASS", "secret")

    executed_sqls: list[str] = []

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.description = [("x",)]
    mock_cursor.fetchmany.return_value = []

    def _capture_execute(sql: str) -> None:
        executed_sqls.append(sql)

    mock_cursor.execute.side_effect = _capture_execute

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch("psycopg2.connect", return_value=mock_conn):
        execute_golden(
            PgwireConfig(pg_host="fake-host"),
            case_id="semi-case",
            expected_sql="SELECT x FROM t ;",
        )

    assert executed_sqls
    assert not executed_sqls[0].rstrip().endswith(";")


# ---------------------------------------------------------------------------
# Bonus: missing env var → OracleError (not exception)
# ---------------------------------------------------------------------------


def test_missing_env_var_returns_oracle_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the password env var is unset, execute_golden returns OracleError."""
    monkeypatch.delenv("ATSCALE_PG_PASS", raising=False)

    result = execute_golden(
        PgwireConfig(pg_host="fake-host"),
        case_id="no-pw",
        expected_sql="SELECT 1",
    )
    assert isinstance(result, OracleError)
    assert result.case_id == "no-pw"
    assert "ATSCALE_PG_PASS" in result.message
