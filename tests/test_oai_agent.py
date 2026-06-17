"""Tests for the OpenAI-compatible MQO agent.

All tests are fully mocked — no live OpenAI endpoint, no live MCP server.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from mqo_eval.agents.oai_agent import OaiAgent, OaiAgentConfig
from mqo_eval.contract import CannotAnswer, HandleAnswer, ScalarAnswer, TabularAnswer

# ---------------------------------------------------------------------------
# Helpers: fake OpenAI response objects
# ---------------------------------------------------------------------------


def _make_stop_response(content: str) -> Any:
    """Fake a chat completion response with finish_reason=stop and text content."""
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _make_tool_call_response(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Fake a chat completion that issues a tool call."""
    import json

    tc = SimpleNamespace(
        id="tc-1",
        function=SimpleNamespace(name=tool_name, arguments=json.dumps(arguments)),
    )
    msg = SimpleNamespace(content=None, tool_calls=[tc])
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice])


# ---------------------------------------------------------------------------
# Test 1: config defaults
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    """OaiAgentConfig has expected defaults; api_key_env is a name not a value."""
    cfg = OaiAgentConfig()
    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.model == "llama3"
    assert cfg.api_key_env == "OAI_API_KEY"
    assert cfg.max_turns == 8
    assert cfg.timeout_s == 120.0
    assert cfg.catalog_path == ""
    # api_key_env must look like an env var name, not an actual key
    assert " " not in cfg.api_key_env
    assert not cfg.api_key_env.startswith("sk-")


# ---------------------------------------------------------------------------
# Test 2: turn cap emits CannotAnswer
# ---------------------------------------------------------------------------


def test_turn_cap_emits_cannot_answer() -> None:
    """Mock LLM that always returns a tool call → CannotAnswer after max_turns."""
    cfg = OaiAgentConfig(max_turns=3)
    agent = OaiAgent(cfg)

    tool_resp = _make_tool_call_response(
        "query_multidimensional", {"query": "total sales"}
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = tool_resp

    mock_mcp = MagicMock()
    mock_mcp.call_tool.return_value = {"result": "some data"}  # no handle_id

    with patch.object(agent, "_run_loop", wraps=agent._run_loop) as _wrapped:
        result = agent._run_loop(mock_client, mock_mcp, "total sales?", "", "tpcds")

    assert isinstance(result, CannotAnswer)
    assert result.reason == "turn cap"
    # Should have called create exactly max_turns times
    assert mock_client.chat.completions.create.call_count == 3


# ---------------------------------------------------------------------------
# Test 3: tool call dispatched
# ---------------------------------------------------------------------------


def test_tool_call_dispatched() -> None:
    """Mock LLM returns a tool call, mock MCP returns a result → loop continues."""
    cfg = OaiAgentConfig(max_turns=4)
    agent = OaiAgent(cfg)

    # Turn 1: tool call; Turn 2: stop with answer
    stop_resp = _make_stop_response('{"answer_type":"scalar","value":42}')
    tool_resp = _make_tool_call_response("describe_model", {"model_coord": "tpcds"})

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [tool_resp, stop_resp]

    mock_mcp = MagicMock()
    mock_mcp.call_tool.return_value = {"description": "TPC-DS model"}

    result = agent._run_loop(mock_client, mock_mcp, "describe tpcds", "", "tpcds")

    assert isinstance(result, ScalarAnswer)
    assert result.value == 42
    mock_mcp.call_tool.assert_called_once_with(
        "describe_model", {"model_coord": "tpcds"}
    )


# ---------------------------------------------------------------------------
# Test 4: handle in tool result detected
# ---------------------------------------------------------------------------


def test_handle_in_tool_result_detected() -> None:
    """Mock MCP returns handle_id → HandleAnswer emitted immediately."""
    cfg = OaiAgentConfig(max_turns=4)
    agent = OaiAgent(cfg)

    tool_resp = _make_tool_call_response(
        "query_multidimensional", {"query": "big query"}
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = tool_resp

    mock_mcp = MagicMock()
    mock_mcp.call_tool.return_value = {
        "handle_id": "h1",
        "row_count": 50000,
        "session_id": "sess-123",
    }

    result = agent._run_loop(mock_client, mock_mcp, "big query?", "", "tpcds")

    assert isinstance(result, HandleAnswer)
    assert result.handle_id == "h1"
    assert result.resolve["row_count"] == 50000


# ---------------------------------------------------------------------------
# Test 5: inline rows detected
# ---------------------------------------------------------------------------


def test_inline_rows_detected() -> None:
    """Mock MCP returns columns/rows → TabularAnswer emitted."""
    cfg = OaiAgentConfig(max_turns=4)
    agent = OaiAgent(cfg)

    tool_resp = _make_tool_call_response(
        "query_multidimensional", {"query": "small query"}
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = tool_resp

    mock_mcp = MagicMock()
    mock_mcp.call_tool.return_value = {
        "columns": ["store_id", "revenue"],
        "rows": [[1, 9999.0], [2, 8888.0]],
    }

    result = agent._run_loop(mock_client, mock_mcp, "small query?", "", "tpcds")

    assert isinstance(result, TabularAnswer)
    assert result.columns == ["store_id", "revenue"]
    assert len(result.rows) == 2


# ---------------------------------------------------------------------------
# Test 6: cannot_answer from model
# ---------------------------------------------------------------------------


def test_cannot_answer_from_model() -> None:
    """Mock LLM emits cannot_answer JSON → CannotAnswer."""
    cfg = OaiAgentConfig(max_turns=4)
    agent = OaiAgent(cfg)

    stop_resp = _make_stop_response(
        '{"answer_type":"cannot_answer","reason":"insufficient data"}'
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = stop_resp

    mock_mcp = MagicMock()

    result = agent._run_loop(mock_client, mock_mcp, "unanswerable?", "", "")

    assert isinstance(result, CannotAnswer)
    assert result.reason == "insufficient data"


# ---------------------------------------------------------------------------
# Test 7: no API key in logs
# ---------------------------------------------------------------------------


def test_no_api_key_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Agent with a configured key env var doesn't log the key value."""
    import os

    secret_key = "sk-verysecretkey12345"
    os.environ["TEST_OAI_KEY"] = secret_key

    cfg = OaiAgentConfig(api_key_env="TEST_OAI_KEY", max_turns=1)
    agent = OaiAgent(cfg)

    stop_resp = _make_stop_response('{"answer_type":"scalar","value":"ok"}')
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = stop_resp
    mock_mcp = MagicMock()

    import logging

    with caplog.at_level(logging.DEBUG):
        agent._run_loop(mock_client, mock_mcp, "test question?", "", "")

    # The secret key must not appear in any log record
    for record in caplog.records:
        assert secret_key not in record.getMessage(), (
            f"Secret key leaked in log: {record.getMessage()}"
        )

    # Also verify api_key_env stores the NAME, not the value
    assert cfg.api_key_env == "TEST_OAI_KEY"
    assert cfg.api_key_env != secret_key

    del os.environ["TEST_OAI_KEY"]


# ---------------------------------------------------------------------------
# Test 8: agent registered in agents.yaml
# ---------------------------------------------------------------------------


def test_agent_registered_in_yaml() -> None:
    """agents.yaml has an oai-agent entry with expected capabilities."""
    agents_yaml = Path(__file__).parent.parent / "agents.yaml"
    assert agents_yaml.exists(), "agents.yaml not found"

    with agents_yaml.open() as f:
        data = yaml.safe_load(f)

    agents = data.get("agents", {})
    assert "oai-agent" in agents, (
        f"oai-agent not in agents.yaml; found: {list(agents.keys())}"
    )

    entry = agents["oai-agent"]
    assert entry.get("kind") == "subprocess"
    caps = entry.get("capabilities", {})
    assert caps.get("returns_handle") is True, (
        "oai-agent must declare returns_handle: true"
    )
