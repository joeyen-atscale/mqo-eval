"""Claude-OAuth agent — headless Claude via subscription, no API key."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mqo_eval.contract import AgentAnswer, CannotAnswer, ParseError, parse_answer

# The mqo-mcp-server binary path
MQO_MCP_BINARY = os.environ.get("MQO_MCP_BINARY", "mqo-mcp-server")

# Default catalog fixture path
DEFAULT_CATALOG = str(
    Path(__file__).parent.parent.parent.parent
    / "mqo-mcp-server" / "fixtures" / "tpcds_catalog.json"
)

ANSWER_SCHEMA_PROMPT = """
At the end of your response, you MUST emit a JSON object on its own line matching this schema:
{"answer_type": "tabular", "columns": [...], "rows": [[...]]}
OR {"answer_type": "scalar", "value": <value>}
OR {"answer_type": "handle", "handle_id": "<id>", "resolve": {}}
OR {"answer_type": "cannot_answer", "reason": "<reason>"}

Emit ONLY the JSON object as the last line of your response. No markdown fences around it.
"""


@dataclass
class ClaudeOAuthConfig:
    catalog_path: str = ""          # path to mqo-mcp-server catalog JSON
    # Sonnet by default: Haiku can't reliably drive the MQO MCP tools (confabulates
    # infra errors → ~10%); Sonnet works (~80%). Override via MQO_CLAUDE_MODEL.
    model: str = field(default_factory=lambda: os.environ.get("MQO_CLAUDE_MODEL", "claude-sonnet-4-6"))
    timeout_s: float = 600.0
    mcp_timeout_ms: int = 60_000    # MCP_TIMEOUT env var for stdio server startup
    extra_args: list[str] = field(default_factory=list)


def _build_mcp_config(cfg: ClaudeOAuthConfig) -> dict[str, Any]:
    """Build the MCP server config dict for mqo-mcp-server.

    Fixture mode (default): just ``--catalog <snapshot>``.
    Live mode (when ``MQO_ENDPOINT`` is set): add ``--endpoint`` + OIDC flags so
    the server queries the live AtScale cluster — required when the gold oracle
    scores against live data (the fixture data is synthetic and won't match).
    """
    catalog = cfg.catalog_path or os.environ.get("MQO_CATALOG_PATH", "")
    args = ["--catalog", catalog] if catalog else []

    endpoint = os.environ.get("MQO_ENDPOINT", "")
    if endpoint:
        args += ["--endpoint", endpoint]
        for env_key, flag in [
            ("MQO_XMLA_URL", "--xmla-url"),
            ("MQO_OIDC_TOKEN_URL", "--oidc-token-url"),
            ("MQO_OIDC_CLIENT_ID", "--oidc-client-id"),
            ("MQO_OIDC_REALM", "--oidc-realm"),
        ]:
            val = os.environ.get(env_key, "")
            if val:
                args += [flag, val]
        # secret passed by env-var NAME, never as a value
        args += ["--oidc-client-secret-env", os.environ.get("MQO_OIDC_SECRET_ENV", "ATSCALE_OIDC_SECRET")]

        # Optional: direct PGWire credentials (CE / no-OIDC-PGWire mode).
        # MQO_PG_USER sets --pg-user; MQO_PG_PASS_ENV sets --pg-pass-env (name of the env var
        # holding the password, not the value itself). Falls back to ATSCALE_PG_USER if set.
        pg_user = os.environ.get("MQO_PG_USER") or os.environ.get("ATSCALE_PG_USER", "")
        if pg_user:
            args += ["--pg-user", pg_user]
        pg_pass_env = os.environ.get("MQO_PG_PASS_ENV", "")
        if pg_pass_env:
            args += ["--pg-pass-env", pg_pass_env]

        # Optional: override backend router for all queries (e.g. "sql" for CE where XMLA is broken).
        force_backend = os.environ.get("MQO_FORCE_BACKEND", "")
        if force_backend:
            args += ["--force-backend", force_backend]

        # Optional: skip the backend capability probe at startup (useful when DAX/MDX are known broken).
        if os.environ.get("MQO_NO_PROBE", "").lower() in ("1", "true", "yes"):
            args += ["--no-probe"]

    return {
        "mcpServers": {
            "mqo": {
                "type": "stdio",
                "command": MQO_MCP_BINARY,
                "args": args,
            }
        }
    }


def _extract_answer_from_result(result_text: str) -> AgentAnswer:
    """Extract the AgentAnswer JSON from Claude's result text.

    Looks for the last line that is a valid JSON object with 'answer_type'.
    Falls back to parse_answer on the whole result.
    """
    lines = result_text.strip().splitlines()
    # Try from the end, find the last line that looks like an answer envelope
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            if "answer_type" in data:
                return parse_answer(line)
        except (json.JSONDecodeError, ParseError):
            continue
    # Fallback: try parse_answer on the entire result
    try:
        return parse_answer(result_text.strip())
    except ParseError:
        return CannotAnswer(reason=f"could not parse answer from: {result_text[:200]!r}")


# ---------------------------------------------------------------------------
# Trace capture (PRD-mqoeval-mqo-trace-capture)
#
# With ``--output-format stream-json --verbose`` Claude emits one JSON object per
# line: a ``system`` init, ``assistant`` messages (whose content holds ``tool_use``
# blocks), ``user`` messages (holding ``tool_result`` blocks), and a final
# ``result`` message. We parse that stream to record every ``mcp__mqo__*`` tool the
# model invoked — crucially the *MQO it sent* to ``query_multidimensional`` and the
# rows/error the server returned — so a failed case can be classified as model-fault
# (wrong MQO) vs MQO-fault (right MQO, wrong rows) at a glance.
# ---------------------------------------------------------------------------

TRACE_RESULT_CAP = 4000  # max chars of a tool_result captured into the trace
TRACE_ROW_CAP = 10       # max rows surfaced from a tool_result


def _stringify_tool_result_content(content: Any) -> tuple[str, list | None]:
    """Flatten an MCP tool_result ``content`` payload to (text, rows?).

    ``content`` may be a string, a list of content blocks
    (``[{"type": "text", "text": ...}]``), or an arbitrary object. If the text
    parses as JSON carrying a ``rows`` list, the first ``TRACE_ROW_CAP`` rows are
    surfaced separately for one-glance triage.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
            elif isinstance(blk, str):
                parts.append(blk)
            else:
                parts.append(json.dumps(blk, default=str))
        text = "\n".join(parts)
    else:
        text = json.dumps(content, default=str)

    rows: list | None = None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
            rows = obj["rows"][:TRACE_ROW_CAP]
    except (json.JSONDecodeError, ValueError):
        pass
    return text, rows


def _extract_bound_sql(text: str) -> str | None:
    """Pull a server-echoed bound SQL/DAX string out of a tool_result, if present.

    mqo-mcp-server (v0.56.0) echoes the compiled query under ``compiled_query`` (SQL
    when the SQL backend is chosen, DAX/MDX for the multidimensional backends). The
    older ``bound_sql``/``dax`` names are kept first for forward/backward compat.
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    for key in ("bound_sql", "compiled_sql", "sql", "bound_dax", "dax",
                "compiled_query", "compiled_dax", "compiled_mdx"):
        val = obj.get(key)
        if val:
            return str(val)
    return None


# Server-side signal fields the mqo-mcp result envelope carries (v0.56.0) that are
# load-bearing for diagnosis but were previously buried inside the truncated result
# blob. Surfacing them as first-class trace fields is the cheap-observability win.
_SIGNAL_KEYS = (
    "backend",            # which backend executed (sql | dax | mdx)
    "routing_reason",     # why the router picked that backend
    "row_count",          # rows the server returned
    "blank_member_rows",  # count of blank/NULL dimension-member rows (fidelity fix)
    "notes",              # advisory strings (e.g. the BLANK MEMBERS guidance)
    "handle",             # dataset handle id (handle-first results)
    "filters_dropped",    # any filters the binder could not honor
)


def _extract_signals(text: str) -> dict[str, Any] | None:
    """Pull the mqo-mcp result-envelope signal fields out of a tool_result."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    sig = {k: obj[k] for k in _SIGNAL_KEYS if obj.get(k) not in (None, [], "")}
    return sig or None


def parse_trace_from_stream(lines: list[str]) -> list[dict[str, Any]]:
    """Parse claude stream-json lines into an ordered MQO trace.

    Returns one entry per ``mcp__mqo__*`` ``tool_use``, with the matching
    ``tool_result`` attached. For ``query_multidimensional`` the sent MQO is stored
    verbatim under the ``mqo`` key (the load-bearing field); other tools store their
    arguments under ``args``.
    """
    pending: dict[str, int] = {}  # tool_use_id -> index in trace
    trace: list[dict[str, Any]] = []
    seq = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        etype = ev.get("type")

        if etype == "assistant" and isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                name = blk.get("name", "")
                if not name.startswith("mcp__mqo__"):
                    continue
                tinput = blk.get("input", {})
                entry: dict[str, Any] = {"seq": seq, "tool": name}
                seq += 1
                if name.endswith("query_multidimensional"):
                    entry["mqo"] = (
                        tinput.get("mqo", tinput)
                        if isinstance(tinput, dict) else tinput
                    )
                else:
                    entry["args"] = tinput
                trace.append(entry)
                tuid = blk.get("id")
                if tuid:
                    pending[tuid] = len(trace) - 1

        elif etype == "user" and isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                    continue
                tuid = blk.get("tool_use_id")
                if tuid is None or tuid not in pending:
                    continue
                entry = trace[pending[tuid]]
                text, rows = _stringify_tool_result_content(blk.get("content"))
                is_error = bool(blk.get("is_error"))
                entry["is_error"] = is_error
                if is_error:
                    entry["error"] = text[:TRACE_RESULT_CAP]
                else:
                    if rows is not None:
                        entry["result_rows"] = rows
                    entry["result"] = text[:TRACE_RESULT_CAP]
                    signals = _extract_signals(text)
                    if signals is not None:
                        entry["signals"] = signals
                bound = _extract_bound_sql(text)
                entry["bound_sql"] = bound  # explicit null when unavailable (G3)

    return trace


def _extract_result_text_from_stream(lines: list[str]) -> tuple[str | None, bool]:
    """Find the final answer text + error flag in a claude stream.

    Handles both ``stream-json`` (a ``type == "result"`` event) and the legacy
    single ``--output-format json`` envelope (a bare object with a ``result`` key),
    so the answer path is robust regardless of output format.
    """
    result_text: str | None = None
    is_error = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result_text = ev.get("result", "")
            is_error = bool(ev.get("is_error"))
        elif "type" not in ev and "result" in ev:
            # Legacy single-envelope (--output-format json)
            result_text = ev.get("result", "")
            is_error = bool(ev.get("is_error"))
    return result_text, is_error


def _write_trace(trace: list[dict[str, Any]]) -> None:
    """Write the captured trace to ``$MQO_TRACE_OUT`` if the runner asked for it."""
    out = os.environ.get("MQO_TRACE_OUT")
    if not out:
        return
    with contextlib.suppress(OSError):
        Path(out).write_text(json.dumps(trace), encoding="utf-8")


class ClaudeOAuthAgent:
    """Drives mqo-mcp tools via headless Claude using the OAuth subscription."""

    def __init__(self, cfg: ClaudeOAuthConfig) -> None:
        self.cfg = cfg

    def answer(self, question: str, context: str, model_coord: str) -> AgentAnswer:
        """Run one NL question through Claude + mqo-mcp tools."""
        prompt = self._build_prompt(question, context, model_coord)
        mcp_config = _build_mcp_config(self.cfg)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="mqo_mcp_cfg_"
        ) as f:
            json.dump(mcp_config, f)
            cfg_path = f.name

        try:
            return self._invoke_claude(prompt, cfg_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(cfg_path)

    def _build_prompt(self, question: str, context: str, model_coord: str) -> str:
        parts = []
        if context:
            parts.append(context.strip())
        parts.append(
            f"Use the mqo-mcp tools to answer the following question about the "
            f"AtScale semantic model '{model_coord}':\n\n{question}"
        )
        return "\n\n".join(parts)

    def _invoke_claude(self, prompt: str, cfg_path: str) -> AgentAnswer:
        # Strip the API key so OAuth subscription is used
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["MCP_TIMEOUT"] = str(self.cfg.mcp_timeout_ms)

        # stream-json + verbose so the per-tool MQO/result events are visible to the
        # trace parser; the final answer rides the closing ``result`` event.
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--mcp-config", cfg_path,
            "--strict-mcp-config",
            "--allowedTools", "mcp__mqo__*",
            "--append-system-prompt", ANSWER_SCHEMA_PROMPT,
        ]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        cmd += self.cfg.extra_args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,  # prevent stdin-pipe inheritance from parent
                text=True,
                env=env,
                timeout=self.cfg.timeout_s,
            )
        except subprocess.TimeoutExpired:
            _write_trace([])  # no stream to capture
            return CannotAnswer(reason=f"claude timed out after {self.cfg.timeout_s}s")

        lines = (result.stdout or "").splitlines()
        # Capture the trace before any early-return so even a non-zero exit (a failed
        # case — exactly when we most want the trace) records what the model did.
        _write_trace(parse_trace_from_stream(lines))

        if result.returncode != 0:
            stderr_snippet = result.stderr[:200] if result.stderr else ""
            return CannotAnswer(reason=f"claude exited {result.returncode}: {stderr_snippet}")

        result_text, is_error = _extract_result_text_from_stream(lines)
        if result_text is None:
            return CannotAnswer(reason=f"claude stdout is not JSON: {result.stdout[:200]!r}")
        if is_error:
            return CannotAnswer(reason=f"claude error: {result_text[:200]}")
        if not result_text:
            return CannotAnswer(reason="claude returned empty result")

        return _extract_answer_from_result(result_text)


def main() -> None:
    """Entry point for use as a subprocess agent via the --agent registry."""
    case_env = os.environ.get("MQO_EVAL_CASE", "{}")
    try:
        case_data = json.loads(case_env)
    except json.JSONDecodeError:
        case_data = {}

    question = case_data.get("question", "")
    context = case_data.get("context", "")
    model_coord = case_data.get("model", "")
    catalog = os.environ.get("MQO_CATALOG_PATH", "")

    if not question:
        print(json.dumps({"answer_type": "cannot_answer", "reason": "no question in MQO_EVAL_CASE"}))
        return

    cfg = ClaudeOAuthConfig(catalog_path=catalog)
    agent = ClaudeOAuthAgent(cfg)
    ans = agent.answer(question, context, model_coord)
    print(ans.model_dump_json())


if __name__ == "__main__":
    main()
