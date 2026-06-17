"""Tests for the Claude-OAuth headless agent.

All tests are fully mocked — no live claude invocation, no live MCP server.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from mqo_eval.agents.claude_oauth_agent import (
    ClaudeOAuthAgent,
    ClaudeOAuthConfig,
    _extract_answer_from_result,
)
from mqo_eval.contract import CannotAnswer, TabularAnswer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed_process(
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    """Build a fake subprocess.CompletedProcess-like object."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _claude_json_envelope(result_text: str, is_error: bool = False) -> str:
    """Wrap result_text in the expected claude --output-format json envelope."""
    return json.dumps({"result": result_text, "is_error": is_error})


# ---------------------------------------------------------------------------
# Test 1: ANTHROPIC_API_KEY stripped from child env
# ---------------------------------------------------------------------------


def test_api_key_stripped_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess.run must be called with an env dict that has no ANTHROPIC_API_KEY."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    tabular_json = json.dumps(
        {"answer_type": "tabular", "columns": ["x"], "rows": [[1]]}
    )
    fake_stdout = _claude_json_envelope(tabular_json)

    with patch("subprocess.run", return_value=_make_completed_process(fake_stdout)) as mock_run:
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        agent.answer("how many stores?", "", "tpcds")

    _env = mock_run.call_args.kwargs.get("env") or mock_run.call_args[1].get("env")
    assert _env is not None, "env must be passed explicitly"
    assert "ANTHROPIC_API_KEY" not in _env


# ---------------------------------------------------------------------------
# Test 2: --mcp-config and --strict-mcp-config in cmd
# ---------------------------------------------------------------------------


def test_mcp_config_written_to_tempfile() -> None:
    """The command must include --mcp-config <path> and --strict-mcp-config."""
    tabular_json = json.dumps(
        {"answer_type": "tabular", "columns": [], "rows": []}
    )
    fake_stdout = _claude_json_envelope(tabular_json)

    with patch("subprocess.run", return_value=_make_completed_process(fake_stdout)) as mock_run:
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        agent.answer("test question", "", "tpcds")

    cmd = mock_run.call_args[0][0]
    assert "--mcp-config" in cmd
    assert "--strict-mcp-config" in cmd
    # The mcp-config arg value should be a file path string (not empty)
    mcp_idx = cmd.index("--mcp-config")
    assert mcp_idx + 1 < len(cmd)
    cfg_path = cmd[mcp_idx + 1]
    assert cfg_path.endswith(".json")


# ---------------------------------------------------------------------------
# Test 3: --allowedTools mcp__mqo__* in cmd
# ---------------------------------------------------------------------------


def test_allowed_tools_mcp_mqo_star() -> None:
    """The command must include --allowedTools mcp__mqo__*."""
    tabular_json = json.dumps(
        {"answer_type": "tabular", "columns": [], "rows": []}
    )
    fake_stdout = _claude_json_envelope(tabular_json)

    with patch("subprocess.run", return_value=_make_completed_process(fake_stdout)) as mock_run:
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        agent.answer("test question", "", "tpcds")

    cmd = mock_run.call_args[0][0]
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "mcp__mqo__*"


# ---------------------------------------------------------------------------
# Test 4: Tabular answer parsed correctly
# ---------------------------------------------------------------------------


def test_tabular_answer_parsed() -> None:
    """When Claude returns a tabular envelope, we get back a TabularAnswer."""
    tabular_json = json.dumps(
        {"answer_type": "tabular", "columns": ["store_id", "total"], "rows": [["S1", 100]]}
    )
    fake_stdout = _claude_json_envelope(tabular_json)

    with patch("subprocess.run", return_value=_make_completed_process(fake_stdout)):
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        result = agent.answer("sales by store?", "", "tpcds")

    assert isinstance(result, TabularAnswer)
    assert result.columns == ["store_id", "total"]
    assert result.rows == [["S1", 100]]


# ---------------------------------------------------------------------------
# Test 5: Timeout → CannotAnswer
# ---------------------------------------------------------------------------


def test_cannot_answer_on_timeout() -> None:
    """TimeoutExpired from subprocess yields CannotAnswer with 'timed out' in reason."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=120)):
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig(timeout_s=120.0))
        result = agent.answer("any question", "", "tpcds")

    assert isinstance(result, CannotAnswer)
    assert "timed out" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test 6: Non-zero exit → CannotAnswer
# ---------------------------------------------------------------------------


def test_cannot_answer_on_nonzero_exit() -> None:
    """Non-zero returncode yields CannotAnswer with exit code in reason."""
    with patch(
        "subprocess.run",
        return_value=_make_completed_process("", returncode=1, stderr="error msg"),
    ):
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        result = agent.answer("any question", "", "tpcds")

    assert isinstance(result, CannotAnswer)
    assert "1" in result.reason


# ---------------------------------------------------------------------------
# Test 7: Empty result → CannotAnswer
# ---------------------------------------------------------------------------


def test_cannot_answer_on_empty_result() -> None:
    """Empty result field in the envelope yields CannotAnswer."""
    fake_stdout = json.dumps({"result": "", "is_error": False})

    with patch("subprocess.run", return_value=_make_completed_process(fake_stdout)):
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        result = agent.answer("any question", "", "tpcds")

    assert isinstance(result, CannotAnswer)
    assert "empty" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test 8: agents.yaml registers claude-oauth with requires_api: false
# ---------------------------------------------------------------------------


def test_agent_registered_in_yaml() -> None:
    """agents.yaml must have a 'claude-oauth' entry with requires_api: false."""
    yaml_path = Path(__file__).parent.parent / "agents.yaml"
    assert yaml_path.exists(), f"agents.yaml not found at {yaml_path}"
    data = yaml.safe_load(yaml_path.read_text())
    agents = data.get("agents", {})
    assert "claude-oauth" in agents, "claude-oauth not registered in agents.yaml"
    caps = agents["claude-oauth"].get("capabilities", {})
    assert caps.get("requires_api") is False, "requires_api must be false for claude-oauth"


# ---------------------------------------------------------------------------
# Test 9: Extract answer from multiline result (prose + JSON last line)
# ---------------------------------------------------------------------------


def test_extract_answer_from_multiline_result() -> None:
    """When result has prose followed by JSON on the last line, extract the JSON."""
    tabular_json = {"answer_type": "tabular", "columns": ["n"], "rows": [[42]]}
    multiline_result = (
        "I used the mqo-mcp tools to query the model and got the following result.\n"
        "The store count is 42.\n"
        + json.dumps(tabular_json)
    )
    result = _extract_answer_from_result(multiline_result)
    assert isinstance(result, TabularAnswer)
    assert result.rows == [[42]]
