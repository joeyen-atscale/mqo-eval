"""Tests for MQO trace capture (PRD-mqoeval-mqo-trace-capture).

Covers the stream-json trace parser (AC1/AC2/AC6), the agent's side-channel trace
write, the runner persistence gate (AC3/AC5), and the record byte-compatibility rule
(AC3). All mocked — no live claude, no live MCP server, no live oracle.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from mqo_eval.agents.claude_oauth_agent import (
    ClaudeOAuthAgent,
    ClaudeOAuthConfig,
    _extract_bound_sql,
    _extract_result_text_from_stream,
    parse_trace_from_stream,
)
from mqo_eval.contract import TabularAnswer

CORPUS = Path(__file__).parent.parent / "corpus" / "tpcds_sql_derived_limited.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _assistant_tool_use(tool_use_id: str, name: str, tinput: dict) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "thinking..."},
                    {"type": "tool_use", "id": tool_use_id, "name": name, "input": tinput},
                ]
            },
        }
    )


def _user_tool_result(tool_use_id: str, content, is_error: bool = False) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": is_error,
                        "content": content,
                    }
                ]
            },
        }
    )


def _result_event(answer: dict) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(answer)}
    )


# A realistic happy-path stream: init → assistant tool_use → tool_result → result.
QMD = "mcp__mqo__query_multidimensional"
MQO_OK = {"model": "tpcds", "measures": ["Total Net Profit"], "dimensions": ["Product"]}
ANSWER_OK = {"answer_type": "tabular", "columns": ["Product", "Total Net Profit"],
             "rows": [["A", 10], ["B", 20]]}
STREAM_OK = [
    json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
    _assistant_tool_use("toolu_1", QMD, {"mqo": MQO_OK}),
    _user_tool_result(
        "toolu_1",
        [{"type": "text", "text": json.dumps({"columns": ["Product", "Total Net Profit"],
                                              "rows": [["A", 10], ["B", 20]]})}],
    ),
    _result_event(ANSWER_OK),
]


# ---------------------------------------------------------------------------
# AC6 — synthetic stream parses into the expected trace entry
# ---------------------------------------------------------------------------


def test_parse_trace_query_multidimensional() -> None:
    trace = parse_trace_from_stream(STREAM_OK)
    assert len(trace) == 1
    e = trace[0]
    assert e["tool"] == QMD
    # AC1: the full MQO is stored verbatim under the `mqo` key
    assert e["mqo"] == MQO_OK
    assert e["is_error"] is False
    assert e["result_rows"] == [["A", 10], ["B", 20]]
    assert e["bound_sql"] is None  # server didn't echo SQL (G3 records null)
    assert e["seq"] == 0


def test_parse_trace_unwrapped_mqo_falls_back_to_input() -> None:
    """If the tool input isn't wrapped under `mqo`, the whole input is the MQO."""
    stream = [_assistant_tool_use("t", QMD, {"measures": ["X"], "dimensions": ["Y"]})]
    trace = parse_trace_from_stream(stream)
    assert trace[0]["mqo"] == {"measures": ["X"], "dimensions": ["Y"]}


# ---------------------------------------------------------------------------
# AC2 — the trace makes a model-fault (wrong measure) visible at a glance
# ---------------------------------------------------------------------------


def test_trace_surfaces_wrong_measure_model_fault() -> None:
    wrong = {"model": "tpcds", "measures": ["Catalog Net Profit Amount"],
             "dimensions": ["Product"]}
    stream = [_assistant_tool_use("t", QMD, {"mqo": wrong})]
    trace = parse_trace_from_stream(stream)
    assert trace[0]["mqo"]["measures"] == ["Catalog Net Profit Amount"]


# ---------------------------------------------------------------------------
# tool_result variants: error, string content, bound SQL echo
# ---------------------------------------------------------------------------


def test_parse_trace_error_result() -> None:
    stream = [
        _assistant_tool_use("t2", QMD, {"mqo": {"x": 1}}),
        _user_tool_result("t2", "not_an_mqo: flat fields rejected", is_error=True),
    ]
    e = parse_trace_from_stream(stream)[0]
    assert e["is_error"] is True
    assert "not_an_mqo" in e["error"]
    assert "result_rows" not in e


def test_parse_trace_captures_bound_sql_when_server_echoes_it() -> None:
    payload = {"columns": ["n"], "rows": [[1]], "bound_sql": "SELECT count(*) FROM s"}
    stream = [
        _assistant_tool_use("t3", QMD, {"mqo": {"x": 1}}),
        _user_tool_result("t3", [{"type": "text", "text": json.dumps(payload)}]),
    ]
    e = parse_trace_from_stream(stream)[0]
    assert e["bound_sql"] == "SELECT count(*) FROM s"


def test_non_mqo_tools_are_ignored() -> None:
    stream = [_assistant_tool_use("t", "Read", {"file": "x"})]
    assert parse_trace_from_stream(stream) == []


def test_extract_bound_sql_variants() -> None:
    assert _extract_bound_sql(json.dumps({"dax": "EVALUATE ..."})) == "EVALUATE ..."
    assert _extract_bound_sql(json.dumps({"rows": []})) is None
    assert _extract_bound_sql("not json") is None


def test_parse_trace_tolerates_noise_lines() -> None:
    """Non-JSON / malformed lines are skipped, not fatal."""
    stream = ["", "not json at all", *STREAM_OK]
    assert len(parse_trace_from_stream(stream)) == 1


# ---------------------------------------------------------------------------
# Answer extraction works for both stream-json and the legacy envelope
# ---------------------------------------------------------------------------


def test_extract_result_from_stream() -> None:
    text, is_error = _extract_result_text_from_stream(STREAM_OK)
    assert is_error is False
    assert json.loads(text)["answer_type"] == "tabular"


def test_extract_result_from_legacy_envelope() -> None:
    legacy = [json.dumps({"result": json.dumps(ANSWER_OK), "is_error": False})]
    text, is_error = _extract_result_text_from_stream(legacy)
    assert is_error is False
    assert json.loads(text)["rows"] == [["A", 10], ["B", 20]]


# ---------------------------------------------------------------------------
# Agent end-to-end (mocked subprocess): writes trace file + parses answer
# ---------------------------------------------------------------------------


def test_agent_writes_trace_file_and_parses_answer(tmp_path, monkeypatch) -> None:
    trace_out = tmp_path / "trace.json"
    monkeypatch.setenv("MQO_TRACE_OUT", str(trace_out))
    stdout = "\n".join(STREAM_OK)
    with patch("subprocess.run", return_value=_proc(stdout)):
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        ans = agent.answer("net profit per product", "", "tpcds")
    assert isinstance(ans, TabularAnswer)
    assert ans.rows == [["A", 10], ["B", 20]]
    written = json.loads(trace_out.read_text())
    assert written[0]["tool"] == QMD
    assert written[0]["mqo"]["measures"] == ["Total Net Profit"]


def test_agent_command_uses_stream_json(monkeypatch) -> None:
    stdout = "\n".join(STREAM_OK)
    with patch("subprocess.run", return_value=_proc(stdout)) as mock_run:
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        agent.answer("q", "", "tpcds")
    cmd = mock_run.call_args[0][0]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd


def test_agent_no_trace_file_when_env_unset(monkeypatch) -> None:
    """Without MQO_TRACE_OUT the agent must not error nor write anywhere."""
    monkeypatch.delenv("MQO_TRACE_OUT", raising=False)
    stdout = "\n".join(STREAM_OK)
    with patch("subprocess.run", return_value=_proc(stdout)):
        agent = ClaudeOAuthAgent(ClaudeOAuthConfig())
        ans = agent.answer("q", "", "tpcds")
    assert isinstance(ans, TabularAnswer)


# ---------------------------------------------------------------------------
# AC3 — record byte-compatibility: trace key absent when None, present when set
# ---------------------------------------------------------------------------


def test_record_strips_none_trace_keeps_present() -> None:
    from mqo_eval.record import CaseRecord, RunRecord

    rec = RunRecord(
        run_id="x", started_at="a", finished_at="b",
        agent="ag", server="s", corpus_id="c", config={},
    )
    sample_trace = [{"seq": 0, "tool": QMD, "mqo": {"a": 1}}]
    rec.cases = [
        CaseRecord(id="c1", nl_query="q", verdict="correct"),
        CaseRecord(id="c2", nl_query="q", verdict="wrong", trace=sample_trace),
    ]
    d = rec.to_dict()
    c1 = next(c for c in d["cases"] if c["id"] == "c1")
    c2 = next(c for c in d["cases"] if c["id"] == "c2")
    assert "trace" not in c1  # AC3: absent, not null
    assert c2["trace"] == sample_trace


# ---------------------------------------------------------------------------
# Runner persistence gate (AC5 / AC3): failing always traces; correct gated
# ---------------------------------------------------------------------------


def _tabular(cols, rows) -> str:
    return json.dumps({"answer_type": "tabular", "columns": cols, "rows": rows})


def _ref(cols, rows):
    from mqo_eval.oracle_pgwire import ReferenceTable

    return ReferenceTable(columns=cols, rows=rows)


def _run(tmp_path, *, answer, ref, fake_trace, config_extra):
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus

    base = {
        "oracle": "pgwire", "pg_host": "h", "pg_pass_env": "X",
        "catalog_name": "c", "model_name": "m", "pass_threshold": 0.95, "repeat": 1,
    }
    base.update(config_extra)
    with (
        patch("mqo_eval.runner.pgwire_precheck"),
        patch("mqo_eval.runner.execute_golden", return_value=ref),
        patch("mqo_eval.runner._invoke_agent", return_value=answer),
        patch("mqo_eval.runner._read_trace_file", return_value=fake_trace),
    ):
        corpus = load_corpus(CORPUS)
        agent = AgentEntry(name="t", command="ignored")
        record, _ = run_corpus(corpus, agent, "m", "pgwire", tmp_path / "r", base)
    return record


FAKE_TRACE = [{"seq": 0, "tool": QMD, "mqo": {"m": 1}, "result": "rows", "bound_sql": None}]


def test_failing_case_carries_trace_without_flag(tmp_path) -> None:
    """AC5: verdict != correct → trace persisted even with --trace off."""
    record = _run(
        tmp_path,
        answer=_tabular(["x"], [[9], [9], [9]]),
        ref=_ref(["x"], [[1], [2], [3]]),
        fake_trace=FAKE_TRACE,
        config_extra={"trace": False},
    )
    wrong = [c for c in record.cases if c.verdict == "wrong"]
    assert wrong
    assert all(c.trace == FAKE_TRACE for c in wrong)


def test_correct_case_has_no_trace_when_flag_off(tmp_path) -> None:
    """AC3: trace off + correct verdict → no trace persisted (key will be absent)."""
    record = _run(
        tmp_path,
        answer=_tabular(["col1"], [[42]]),
        ref=_ref(["col1"], [[42]]),
        fake_trace=FAKE_TRACE,
        config_extra={"trace": False},
    )
    correct = [c for c in record.cases if c.verdict == "correct"]
    assert correct
    assert all(c.trace is None for c in correct)


def test_correct_case_carries_trace_when_flag_on(tmp_path) -> None:
    """--trace on → every case (even correct) carries its trace."""
    record = _run(
        tmp_path,
        answer=_tabular(["col1"], [[42]]),
        ref=_ref(["col1"], [[42]]),
        fake_trace=FAKE_TRACE,
        config_extra={"trace": True},
    )
    correct = [c for c in record.cases if c.verdict == "correct"]
    assert correct
    assert all(c.trace == FAKE_TRACE for c in correct)


def test_trace_serializes_in_written_record(tmp_path) -> None:
    """The on-disk record JSON carries trace for failing cases and is valid JSON."""
    record = _run(
        tmp_path,
        answer=_tabular(["x"], [[9]]),
        ref=_ref(["x"], [[1], [2], [3]]),
        fake_trace=FAKE_TRACE,
        config_extra={"trace": False},
    )
    from mqo_eval.record import write_record

    dest = write_record(record, tmp_path / "archive")
    parsed = json.loads(dest.read_text())
    wrong = [c for c in parsed["cases"] if c["verdict"] == "wrong"]
    assert wrong
    assert wrong[0]["trace"] == FAKE_TRACE
    # correct/none cases must NOT carry the key
    skipped = [c for c in parsed["cases"] if c["verdict"] == "skipped"]
    if skipped:
        assert "trace" not in skipped[0]
