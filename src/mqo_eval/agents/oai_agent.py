"""OpenAI-compatible MQO agent — no Anthropic API, handles handles.

Drives one NL question through an OpenAI chat-completions + tool-calling loop.
Dispatches tool calls to mqo-mcp-server via JSON-RPC stdio transport.
Emits an AgentAnswer contract envelope on stdout.

Usage (as subprocess):
    python -m mqo_eval.agents.oai_agent \\
        --question "..." --context "..." --model-coord "..." \\
        [--base-url http://localhost:11434/v1] [--model llama3] \\
        [--api-key-env OAI_API_KEY] [--max-turns 8] [--catalog /path/to/catalog]
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from mqo_eval.contract import (
    AgentAnswer,
    CannotAnswer,
    HandleAnswer,
    ScalarAnswer,
    TabularAnswer,
)

logger = logging.getLogger(__name__)

MQO_MCP_BINARY = os.environ.get("MQO_MCP_BINARY", "mqo-mcp-server")

# Hardcoded tool schemas for the 3 main mqo-mcp tools (OpenAI format)
_MQO_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_multidimensional",
            "description": (
                "Execute a multidimensional query against the semantic layer. "
                "Returns either inline rows or a handle_id for large results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural language or structured query expression."
                        ),
                    },
                    "model_coord": {
                        "type": "string",
                        "description": "Model coordinate (e.g. 'tpcds.store_sales').",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_model",
            "description": (
                "Describe the semantic model: measures, dimensions, "
                "hierarchies, and filters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_coord": {
                        "type": "string",
                        "description": "Model coordinate to describe.",
                    },
                },
                "required": ["model_coord"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_columns",
            "description": (
                "Search for columns/attributes by keyword across the semantic model."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Keyword to search for in column names and descriptions."
                        ),
                    },
                    "model_coord": {
                        "type": "string",
                        "description": "Optional model coordinate to restrict search.",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
]


@dataclass
class OaiAgentConfig:
    """Configuration for the OpenAI-compatible MQO agent."""

    base_url: str = "http://localhost:11434/v1"  # Ollama default
    model: str = "llama3"
    api_key_env: str = "OAI_API_KEY"  # env var NAME, never the value
    max_turns: int = 8
    timeout_s: float = 120.0
    catalog_path: str = ""  # passed to mqo-mcp-server --catalog


class McpStdioTransport:
    """Thin JSON-RPC stdio bridge to mqo-mcp-server.

    Starts the server subprocess and holds it for the duration of a case so
    that handles returned by the server remain resolvable (FR4).
    """

    def __init__(self, catalog_path: str = "") -> None:
        self._catalog_path = catalog_path
        self._proc: subprocess.Popen[str] | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start mqo-mcp-server subprocess."""
        cmd = [MQO_MCP_BINARY]
        if self._catalog_path:
            cmd += ["--catalog", self._catalog_path]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._initialize()
        except FileNotFoundError:
            logger.warning("mqo-mcp-server not found; tool calls will fail gracefully")
            self._proc = None

    def _initialize(self) -> None:
        """Send MCP initialize handshake."""
        if self._proc is None:
            return
        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "oai-agent", "version": "0.1.0"},
            },
        }
        self._send(init_req)
        resp = self._recv()
        if resp and "result" in resp:
            # Send initialized notification
            notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            self._send(notif)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool via JSON-RPC. Returns the result dict or error."""
        if self._proc is None or self._proc.poll() is not None:
            return {"error": "mcp server not running"}
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        self._send(req)
        resp = self._recv()
        if resp is None:
            return {"error": "no response from mcp server"}
        if "error" in resp:
            return {"error": str(resp["error"])}
        return dict(resp.get("result", {}))

    def stop(self) -> None:
        """Terminate the MCP server subprocess."""
        import contextlib

        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
                self._proc.wait(timeout=5)
            with contextlib.suppress(Exception):
                self._proc.kill()
            self._proc = None

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _send(self, obj: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        line = json.dumps(obj) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except BrokenPipeError:
            logger.warning("MCP server stdin broken pipe")

    def _recv(self) -> dict[str, Any] | None:
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            line = self._proc.stdout.readline()
            if not line:
                return None
            return dict(json.loads(line))
        except (json.JSONDecodeError, OSError):
            return None


class OaiAgent:
    """Drives one NL question through an OpenAI-compatible + MCP tool loop."""

    def __init__(self, cfg: OaiAgentConfig) -> None:
        self.cfg = cfg

    def answer(self, question: str, context: str, model_coord: str) -> AgentAnswer:
        """Run the agent loop for one question. Returns an AgentAnswer."""
        if not question or not question.strip():
            return CannotAnswer(reason="empty question")

        # Import here so the module is importable without openai installed in tests
        try:
            import openai
        except ImportError:
            return CannotAnswer(reason="openai package not installed")

        api_key = os.environ.get(self.cfg.api_key_env, "none")
        client = openai.OpenAI(base_url=self.cfg.base_url, api_key=api_key)

        mcp = McpStdioTransport(catalog_path=self.cfg.catalog_path)
        mcp.start()

        try:
            return self._run_loop(client, mcp, question, context, model_coord)
        finally:
            mcp.stop()

    def _run_loop(
        self,
        client: Any,
        mcp: McpStdioTransport,
        question: str,
        context: str,
        model_coord: str,
    ) -> AgentAnswer:
        """Core chat-completions loop with tool dispatch."""
        system_prompt = (
            "You are a data analyst assistant. Use the available tools to answer"
            " the user's question about the semantic data model. When you have"
            " the answer, respond with a JSON object in one of these formats:\n"
            '- Tabular: {"answer_type":"tabular","columns":[...],"rows":[...]}\n'
            '- Handle: {"answer_type":"handle","handle_id":"...","resolve":{}}\n'
            '- Scalar: {"answer_type":"scalar","value":"..."}\n'
            '- Cannot answer: {"answer_type":"cannot_answer","reason":"..."}\n'
            "Do not wrap the JSON in markdown code blocks."
        )
        if context:
            system_prompt += f"\n\nContext: {context}"
        if model_coord:
            system_prompt += f"\n\nModel coordinate: {model_coord}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]

        for _turn in range(self.cfg.max_turns):
            try:
                response = client.chat.completions.create(
                    model=self.cfg.model,
                    messages=messages,
                    tools=_MQO_TOOLS,
                    tool_choice="auto",
                    timeout=self.cfg.timeout_s,
                )
            except Exception as exc:
                logger.warning("LLM call failed: %s", type(exc).__name__)
                return CannotAnswer(reason=f"llm error: {type(exc).__name__}")

            choice = response.choices[0]
            msg = choice.message

            # Accumulate assistant message
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
            }
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            # If stop — parse final answer
            if choice.finish_reason == "stop" or not msg.tool_calls:
                return self._parse_final_answer(msg.content or "")

            # Dispatch tool calls
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}

                tool_result = mcp.call_tool(tool_name, arguments)

                # Handle detection: if the tool returned a handle, emit immediately
                if "handle_id" in tool_result:
                    mcp.stop()  # stop will be called again in finally but idempotent
                    return HandleAnswer(
                        handle_id=str(tool_result["handle_id"]),
                        resolve=dict(tool_result),
                    )

                # If inline rows returned
                if "columns" in tool_result and "rows" in tool_result:
                    return TabularAnswer(
                        columns=list(tool_result["columns"]),
                        rows=list(tool_result["rows"]),
                    )

                tool_result_str = json.dumps(tool_result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result_str,
                    }
                )

        # Turn cap exceeded
        return CannotAnswer(reason="turn cap")

    def _parse_final_answer(self, content: str) -> AgentAnswer:
        """Parse the model's final text response into an AgentAnswer."""
        content = content.strip()

        # Try to extract JSON from the response
        # Strip markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            # Remove first and last fence lines
            inner = [ln for ln in lines if not ln.startswith("```")]
            content = "\n".join(inner).strip()

        # Check for explicit cannot_answer signal in text
        if "cannot_answer" in content.lower() and not content.startswith("{"):
            return CannotAnswer(reason="model declined")

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Not JSON — treat as scalar text answer
            if content:
                return ScalarAnswer(value=content)
            return CannotAnswer(reason="empty model response")

        if not isinstance(data, dict):
            return ScalarAnswer(value=data)

        answer_type = data.get("answer_type")

        if answer_type == "handle" and "handle_id" in data:
            return HandleAnswer(
                handle_id=str(data["handle_id"]),
                resolve=dict(data.get("resolve", {})),
            )
        if answer_type == "tabular":
            return TabularAnswer(
                columns=list(data.get("columns", [])),
                rows=list(data.get("rows", [])),
            )
        if answer_type == "scalar":
            return ScalarAnswer(value=data.get("value"))
        if answer_type == "cannot_answer":
            return CannotAnswer(reason=str(data.get("reason", "")))

        # Legacy compat: bare columns/rows
        if "columns" in data and "rows" in data:
            return TabularAnswer(columns=list(data["columns"]), rows=list(data["rows"]))
        if "handle_id" in data:
            return HandleAnswer(handle_id=str(data["handle_id"]), resolve=dict(data))

        return ScalarAnswer(value=data)


def main() -> None:
    """CLI entry point: parse args, run agent, emit JSON to stdout."""
    import argparse

    parser = argparse.ArgumentParser(description="OpenAI-compatible MQO agent")
    parser.add_argument("--question", required=True, help="Natural language question")
    parser.add_argument("--context", default="", help="Optional context string")
    parser.add_argument("--model-coord", default="", help="Model coordinate")
    parser.add_argument(
        "--base-url",
        default=OaiAgentConfig.base_url,
        help="OpenAI-compatible base URL",
    )
    parser.add_argument("--model", default=OaiAgentConfig.model, help="Model name")
    parser.add_argument(
        "--api-key-env",
        default=OaiAgentConfig.api_key_env,
        help="Env var NAME holding the API key (never the key itself)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=OaiAgentConfig.max_turns,
        help="Max turns before cannot_answer",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=OaiAgentConfig.timeout_s,
        help="Per-request timeout in seconds",
    )
    parser.add_argument(
        "--catalog",
        default="",
        help="Path to catalog for mqo-mcp-server",
    )
    args = parser.parse_args()

    cfg = OaiAgentConfig(
        base_url=args.base_url,
        model=args.model,
        api_key_env=args.api_key_env,
        max_turns=args.max_turns,
        timeout_s=args.timeout,
        catalog_path=args.catalog,
    )
    agent = OaiAgent(cfg)
    result = agent.answer(
        question=args.question,
        context=args.context,
        model_coord=args.model_coord,
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
