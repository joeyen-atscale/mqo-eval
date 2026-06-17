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
    model: str = ""  # empty = use claude's default (Opus); set to e.g. "claude-sonnet-4-6" to override
    timeout_s: float = 300.0
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

        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
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
            return CannotAnswer(reason=f"claude timed out after {self.cfg.timeout_s}s")

        if result.returncode != 0:
            stderr_snippet = result.stderr[:200] if result.stderr else ""
            return CannotAnswer(reason=f"claude exited {result.returncode}: {stderr_snippet}")

        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            return CannotAnswer(reason=f"claude stdout is not JSON: {result.stdout[:200]!r}")

        if envelope.get("is_error"):
            return CannotAnswer(reason=f"claude error: {envelope.get('result','')[:200]}")

        result_text = envelope.get("result", "")
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
