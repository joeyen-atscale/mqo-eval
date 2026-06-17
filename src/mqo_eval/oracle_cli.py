"""CLI-subprocess gold oracle for mqo-eval.

Mints a ReferenceTable by shelling out to ``mqo-pg-query`` (a Rust CLI that
handles AtScale PGWire auth via OIDC/TLS) rather than connecting directly
via psycopg2.

``mqo-pg-query`` must be on PATH for live use.  It is not required for
offline/fixture mode, and tests mock ``subprocess.run`` so the binary is
never invoked in CI.

Expected ``mqo-pg-query`` JSON output schema (one JSON object on stdout):

- Success:  ``{"columns": ["c1", ...], "rows": [[val, ...], ...]}``
- Oversize: ``{"oversize": {"observed_at_least": N, "cap": M}}``
- Error:    ``{"error": {"message": "..."}}``

Any non-zero exit code, non-JSON stdout, or missing ``mqo-pg-query`` is
treated as an ``OracleError`` for that case; the run continues.

Connection parameters / OIDC secrets are passed to ``mqo-pg-query`` via
environment variables that *it* reads — this harness never logs or stores
credential values.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .oracle_pgwire import (
    OracleError,
    Oversize,
    ReferenceResult,
    ReferenceTable,
    substitute_placeholders,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CliOracleConfig:
    """Configuration for the CLI oracle backend.

    All credential values are read at runtime from the environment by
    ``mqo-pg-query`` itself; this config only holds names/paths.
    """

    gold_query_cmd: str = "mqo-pg-query"
    """Path or name of the mqo-pg-query binary (default: look up on PATH)."""

    endpoint: str = ""
    """AtScale PGWire endpoint passed as ``--endpoint`` to mqo-pg-query.
    Typically ``host:port``.  Empty string → not passed (binary uses its
    own default / env-var)."""

    catalog_name: str = "atscale_catalogs"
    """Catalog name substituted into ``${CatalogName}`` placeholders."""

    model_name: str = "tpcds_benchmark_model"
    """Model name substituted into ``${ModelName}`` placeholders."""

    timeout_s: int = 120
    """Per-case subprocess timeout (seconds).  On expiry → OracleError."""

    extra_args: list[str] = field(default_factory=list)
    """Additional CLI arguments forwarded verbatim to ``mqo-pg-query``."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_cmd(cfg: CliOracleConfig, sql: str) -> list[str]:
    """Build the subprocess argv for a single SQL execution."""
    cmd: list[str] = [cfg.gold_query_cmd, "--sql", sql]
    if cfg.endpoint:
        cmd += ["--endpoint", cfg.endpoint]
    cmd.extend(cfg.extra_args)
    return cmd


def _parse_stdout(
    raw: str,
    case_id: str,
) -> ReferenceResult:
    """Parse the JSON output of mqo-pg-query into a typed result.

    Handles all three variants plus malformed JSON gracefully.
    """
    raw = raw.strip()
    if not raw:
        return OracleError(case_id=case_id, message="mqo-pg-query produced no output")

    try:
        obj: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return OracleError(
            case_id=case_id,
            message=f"mqo-pg-query stdout is not valid JSON: {exc}",
        )

    if not isinstance(obj, dict):
        return OracleError(
            case_id=case_id,
            message=f"mqo-pg-query JSON is not an object (got {type(obj).__name__})",
        )

    # --- success path ---
    if "columns" in obj and "rows" in obj:
        columns = obj["columns"]
        rows = obj["rows"]
        if not isinstance(columns, list) or not isinstance(rows, list):
            return OracleError(
                case_id=case_id,
                message="mqo-pg-query JSON: 'columns'/'rows' must be arrays",
            )
        # Type-coerce cells: None stays None, numbers stay typed, everything
        # else is stringified (mirrors oracle_pgwire._type_cell for ints/floats/None).
        typed_rows: list[list[Any]] = [
            [_type_cell(cell) for cell in row] for row in rows
        ]
        return ReferenceTable(columns=columns, rows=typed_rows)

    # --- oversize path ---
    if "oversize" in obj:
        info = obj["oversize"]
        if not isinstance(info, dict):
            return OracleError(
                case_id=case_id,
                message="mqo-pg-query JSON: 'oversize' value must be an object",
            )
        try:
            observed = int(info["observed_at_least"])
            cap = int(info["cap"])
        except (KeyError, TypeError, ValueError) as exc:
            return OracleError(
                case_id=case_id,
                message=f"mqo-pg-query JSON: malformed 'oversize' object: {exc}",
            )
        return Oversize(observed_at_least=observed, cap=cap)

    # --- error path ---
    if "error" in obj:
        info = obj["error"]
        if isinstance(info, dict):
            message = str(info.get("message", repr(info)))
        else:
            message = str(info)
        return OracleError(case_id=case_id, message=message)

    # --- unknown schema ---
    keys = list(obj.keys())[:5]
    return OracleError(
        case_id=case_id,
        message=f"mqo-pg-query JSON: unrecognised schema (top-level keys: {keys})",
    )


def _type_cell(value: Any) -> Any:
    """Coerce a JSON-parsed cell value to None | int | float | str.

    JSON numbers arrive as int or float; booleans (JSON true/false) are
    treated as int (0/1); everything else is str.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    return str(value).strip()


# ---------------------------------------------------------------------------
# Pre-check
# ---------------------------------------------------------------------------


def cli_precheck(cfg: CliOracleConfig) -> None:
    """Run ``mqo-pg-query --sql 'SELECT 1'`` to verify the binary and auth.

    Raises ``RuntimeError`` on failure.  The error message names the
    binary path and reason but never includes credential values.

    Must complete in < 10 s (uses a 9-second timeout).
    """
    cmd = _build_cmd(cfg, "SELECT 1")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=9,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"mqo-pg-query pre-check failed: binary not found: {cfg.gold_query_cmd!r}. "
            "Build PRD-mqo-pgwire-query-cli and ensure it is on PATH."
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"mqo-pg-query pre-check timed out after 9 s (cmd={cfg.gold_query_cmd!r})"
        ) from None

    if proc.returncode != 0:
        # First line of stderr — no credentials
        stderr_line = (proc.stderr or "").splitlines()[0] if proc.stderr else "(no stderr)"
        raise RuntimeError(
            f"mqo-pg-query pre-check exited {proc.returncode}: {stderr_line}"
        )

    # Validate that stdout is parseable JSON (not necessarily a table)
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError(
            f"mqo-pg-query pre-check returned empty output (cmd={cfg.gold_query_cmd!r})"
        )

    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"mqo-pg-query pre-check stdout is not valid JSON: {exc}"
        ) from None


# ---------------------------------------------------------------------------
# Oracle entry point
# ---------------------------------------------------------------------------


def execute_golden_cli(
    cfg: CliOracleConfig,
    case_id: str,
    expected_sql: str,
) -> ReferenceResult:
    """Execute *expected_sql* via ``mqo-pg-query`` and return a typed result.

    Substitutes ``${CatalogName}`` and ``${ModelName}`` placeholders before
    invoking the binary.

    Returns:
        ``ReferenceTable``  — rows returned by the query (may be empty)
        ``Oversize``        — server reported more rows than cap
        ``OracleError``     — binary error, timeout, or parse failure
    """
    sql = substitute_placeholders(expected_sql, cfg.catalog_name, cfg.model_name)
    cmd = _build_cmd(cfg, sql)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=cfg.timeout_s,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        return OracleError(
            case_id=case_id,
            message=(
                f"mqo-pg-query binary not found: {cfg.gold_query_cmd!r}. "
                "Build PRD-mqo-pgwire-query-cli and ensure it is on PATH."
            ),
        )
    except subprocess.TimeoutExpired:
        return OracleError(
            case_id=case_id,
            message=f"mqo-pg-query timed out after {cfg.timeout_s} s",
        )

    if proc.returncode != 0:
        stderr_line = (proc.stderr or "").splitlines()[0] if proc.stderr else "(no stderr)"
        return OracleError(
            case_id=case_id,
            message=f"mqo-pg-query exited {proc.returncode}: {stderr_line}",
        )

    return _parse_stdout(proc.stdout or "", case_id)
