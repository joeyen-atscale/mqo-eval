"""PGWire golden-SQL oracle for mqo-eval.

Executes reference SQL over the AtScale PGWire endpoint (direct user creds)
and returns typed reference tables for downstream scoring.  Never routes gold
through the server-under-test.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ReferenceTable:
    """A typed result from executing golden SQL."""

    columns: list[str]
    rows: list[list[Any]]  # None | int | float | str per cell


@dataclass
class Oversize:
    """Returned when fetched rows >= cap; never silently truncated."""

    observed_at_least: int
    cap: int


@dataclass
class OracleError:
    """Per-case execution error; run continues."""

    case_id: str
    message: str


ReferenceResult = ReferenceTable | Oversize | OracleError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class PgwireConfig:
    """Connection parameters for the AtScale PGWire endpoint."""

    pg_host: str
    pg_port: int = 15432
    pg_user: str = "atscale"
    pg_pass_env: str = "ATSCALE_PG_PASS"  # env-var NAME, never the value
    pg_dbname: str = "atscale_catalogs"  # AtScale reads dbname as the catalog
    sslmode: str = "require"  # AtScale PGWire mandates TLS
    catalog_name: str = "atscale_catalogs"
    model_name: str = "tpcds_benchmark_model"
    max_result_rows: int = 50_000
    query_timeout_s: int = 120

    HARD_ROW_CEIL: ClassVar[int] = 200_000

    def __post_init__(self) -> None:
        if self.max_result_rows > self.HARD_ROW_CEIL:
            self.max_result_rows = self.HARD_ROW_CEIL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\$\{(CatalogName|ModelName)\}")


def substitute_placeholders(sql: str, catalog: str, model: str) -> str:
    """Replace ``${CatalogName}`` and ``${ModelName}`` in *sql*."""

    def _replace(m: re.Match[str]) -> str:
        key = m.group(1)
        return catalog if key == "CatalogName" else model

    return _PLACEHOLDER_RE.sub(_replace, sql)


def _strip_trailing_semicolons(sql: str) -> str:
    return sql.rstrip().rstrip(";").rstrip()


def _type_cell(value: Any) -> Any:
    """Map a psycopg2 cell value to None | int | float | str."""
    if value is None:
        return None
    if isinstance(value, bool):
        # booleans are a subclass of int; keep as int (0/1)
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    # Decimal, numeric, etc. → float
    try:
        import decimal

        if isinstance(value, decimal.Decimal):
            return float(value)
    except ImportError:
        pass
    return str(value).strip()


def _safe_host_port(cfg: PgwireConfig) -> str:
    """Return host:port for logging — never includes credentials."""
    return f"{cfg.pg_host}:{cfg.pg_port}"


# ---------------------------------------------------------------------------
# Pre-check
# ---------------------------------------------------------------------------


def pgwire_precheck(cfg: PgwireConfig) -> None:
    """Connect to PGWire and run ``SELECT 1``.

    Raises ``RuntimeError`` on failure.  The error message contains only
    ``host:port`` — never the password or connection string.
    """
    import psycopg2  # type: ignore[import-untyped]

    password = os.environ.get(cfg.pg_pass_env)
    if password is None:
        raise RuntimeError(
            f"PGWire pre-check failed: env var {cfg.pg_pass_env!r} is not set"
        )

    try:
        conn = psycopg2.connect(
            host=cfg.pg_host,
            port=cfg.pg_port,
            user=cfg.pg_user,
            dbname=cfg.pg_dbname,
            sslmode=cfg.sslmode,
            password=password,
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            conn.close()
    except Exception as exc:
        # Sanitise: strip any occurrence of the password from the message.
        raw = str(exc)
        safe = raw.replace(password, "***") if password else raw
        raise RuntimeError(
            f"PGWire pre-check failed ({_safe_host_port(cfg)}): {safe}"
        ) from None


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------


def execute_golden(
    cfg: PgwireConfig,
    case_id: str,
    expected_sql: str,
) -> ReferenceResult:
    """Execute *expected_sql* over PGWire and return a typed result.

    Returns:
        ``ReferenceTable``  — N ≤ cap rows (including empty)
        ``Oversize``        — fetched rows ≥ cap
        ``OracleError``     — SQL / connection error for this case
    """
    import psycopg2

    password = os.environ.get(cfg.pg_pass_env)
    if password is None:
        return OracleError(
            case_id=case_id,
            message=f"env var {cfg.pg_pass_env!r} is not set",
        )

    sql = substitute_placeholders(expected_sql, cfg.catalog_name, cfg.model_name)
    sql = _strip_trailing_semicolons(sql)

    # cap+1 fetch so we can detect oversize without fetching the entire table
    fetch_limit = cfg.max_result_rows + 1

    try:
        conn = psycopg2.connect(
            host=cfg.pg_host,
            port=cfg.pg_port,
            user=cfg.pg_user,
            dbname=cfg.pg_dbname,
            sslmode=cfg.sslmode,
            password=password,
            connect_timeout=5,
            options=f"-c statement_timeout={cfg.query_timeout_s * 1000}",
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns: list[str] = [desc[0] for desc in (cur.description or [])]
                rows_raw = cur.fetchmany(fetch_limit)
        finally:
            conn.close()
    except Exception as exc:
        raw = str(exc)
        safe = raw.replace(password, "***") if password else raw
        return OracleError(case_id=case_id, message=safe)

    if len(rows_raw) >= fetch_limit:
        return Oversize(observed_at_least=fetch_limit, cap=cfg.max_result_rows)

    typed_rows: list[list[Any]] = [
        [_type_cell(cell) for cell in row] for row in rows_raw
    ]
    return ReferenceTable(columns=columns, rows=typed_rows)


# ---------------------------------------------------------------------------
# Connection-reuse variant (optional for callers that manage a session)
# ---------------------------------------------------------------------------


@dataclass
class OracleSession:
    """Holds an open PGWire connection for multi-case reuse.

    Use as a context manager::

        with OracleSession(cfg) as session:
            result = session.execute(case_id, sql)
    """

    cfg: PgwireConfig
    _conn: Any = field(default=None, init=False, repr=False)

    def __enter__(self) -> OracleSession:
        pgwire_precheck(self.cfg)  # fast-fail before case 1
        self._connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self._close()

    def _connect(self) -> None:
        import psycopg2

        password = os.environ.get(self.cfg.pg_pass_env, "")
        self._conn = psycopg2.connect(
            host=self.cfg.pg_host,
            port=self.cfg.pg_port,
            user=self.cfg.pg_user,
            dbname=self.cfg.pg_dbname,
            sslmode=self.cfg.sslmode,
            password=password,
            connect_timeout=5,
            options=f"-c statement_timeout={self.cfg.query_timeout_s * 1000}",
        )

    def _close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    def execute(self, case_id: str, expected_sql: str) -> ReferenceResult:
        """Run one case, reconnecting if the connection was broken."""

        password = os.environ.get(self.cfg.pg_pass_env)
        if password is None:
            return OracleError(
                case_id=case_id,
                message=f"env var {self.cfg.pg_pass_env!r} is not set",
            )

        sql = substitute_placeholders(
            expected_sql, self.cfg.catalog_name, self.cfg.model_name
        )
        sql = _strip_trailing_semicolons(sql)
        fetch_limit = self.cfg.max_result_rows + 1

        try:
            if self._conn is None or self._conn.closed:
                self._connect()
            with self._conn.cursor() as cur:
                cur.execute(sql)
                columns: list[str] = [desc[0] for desc in (cur.description or [])]
                rows_raw = cur.fetchmany(fetch_limit)
        except Exception as exc:
            raw = str(exc)
            safe = raw.replace(password, "***") if password else raw
            self._close()  # drop broken connection so next case reconnects
            return OracleError(case_id=case_id, message=safe)

        if len(rows_raw) >= fetch_limit:
            return Oversize(
                observed_at_least=fetch_limit, cap=self.cfg.max_result_rows
            )

        typed_rows: list[list[Any]] = [
            [_type_cell(cell) for cell in row] for row in rows_raw
        ]
        return ReferenceTable(columns=columns, rows=typed_rows)
