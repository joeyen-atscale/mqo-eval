"""Acceptance tests for mqo-eval harness core (all 9 ACs)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

CORPUS = Path(__file__).parent.parent / "corpus" / "tpcds_sql_derived_limited.yaml"
REPO_ROOT = Path(__file__).parent.parent


# AC-1
def test_ac_1_corpus_load() -> None:
    from mqo_eval.corpus import load_corpus
    c = load_corpus(CORPUS)
    assert len(c.queries) == 21
    assert len(c.active) == 20
    assert len(c.skipped) == 1


# AC-2
@pytest.mark.parametrize("payload,expected_type", [
    ('{"answer_type":"tabular","columns":["a"],"rows":[[1]]}', "tabular"),
    ('{"answer_type":"handle","handle_id":"h1","resolve":{}}', "handle"),
    ('{"answer_type":"scalar","value":42}', "scalar"),
    ('{"answer_type":"cannot_answer","reason":"no path"}', "cannot_answer"),
])
def test_ac_2_contract_roundtrip(payload: str, expected_type: str) -> None:
    from mqo_eval.contract import parse_answer
    answer = parse_answer(payload)
    assert answer.answer_type == expected_type
    back = parse_answer(answer.model_dump_json())
    assert back.answer_type == expected_type


def test_ac_2_malformed_raises_parse_error() -> None:
    from mqo_eval.contract import ParseError, parse_answer
    with pytest.raises(ParseError):
        parse_answer("not json <<<")
    with pytest.raises(ParseError):
        parse_answer('{"answer_type":"unknown_variant"}')
    with pytest.raises(ParseError):
        parse_answer("")


# AC-3
def test_ac_3_stub_run_writes_record(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import load_registry, resolve_agent
    from mqo_eval.runner import run_corpus
    corpus = load_corpus(CORPUS)
    registry = load_registry(REPO_ROOT / "agents.yaml")
    agent = resolve_agent("stub", registry)
    _record, dest = run_corpus(
        corpus, agent, "test_model", "fixture", tmp_path / "results", {"agent": "stub"}
    )
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert data["agent"] == "stub"
    assert data["summary"]["active"] == 20
    assert data["summary"]["skipped"] == 1


# AC-4
def test_ac_4_no_overwrite(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import load_registry, resolve_agent
    from mqo_eval.runner import run_corpus
    corpus = load_corpus(CORPUS)
    registry = load_registry(REPO_ROOT / "agents.yaml")
    agent = resolve_agent("stub", registry)
    _, dest1 = run_corpus(corpus, agent, "m", "fixture", tmp_path / "r", {})
    time.sleep(1.1)
    _, dest2 = run_corpus(corpus, agent, "m", "fixture", tmp_path / "r", {})
    assert dest1 != dest2
    assert dest1.exists() and dest2.exists()


# AC-5
def test_ac_5_unknown_agent_error() -> None:
    from mqo_eval.registry import RegistryError, load_registry, resolve_agent
    registry = load_registry(REPO_ROOT / "agents.yaml")
    with pytest.raises(RegistryError) as exc_info:
        resolve_agent("totally_unknown_xyzzy", registry)
    assert "stub" in str(exc_info.value)


# AC-6
def test_ac_6_all_disabled_no_crash(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import load_registry, resolve_agent
    from mqo_eval.runner import run_corpus
    disabled_corpus = tmp_path / "disabled.yaml"
    disabled_corpus.write_text(
        "context: \"\"\n"
        "queries:\n"
        "  - id: q1\n"
        "    nl_query: test\n"
        "    expected_sql: SELECT 1\n"
        "    disabled: true\n"
    )
    corpus = load_corpus(disabled_corpus)
    assert len(corpus.active) == 0
    registry = load_registry(REPO_ROOT / "agents.yaml")
    agent = resolve_agent("stub", registry)
    record, dest = run_corpus(corpus, agent, "m", "fixture", tmp_path / "r", {})
    assert record.summary.active == 0
    assert record.summary.skipped == 1
    assert dest.exists()


# AC-7
def test_ac_7_malformed_agent_json(tmp_path: Path) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import AgentEntry
    from mqo_eval.runner import run_corpus
    tiny = tmp_path / "tiny.yaml"
    tiny.write_text(
        "context: \"\"\nqueries:\n"
        "  - {id: q1, nl_query: q1, expected_sql: 'SELECT 1'}\n"
        "  - {id: q2, nl_query: q2, expected_sql: 'SELECT 2'}\n"
    )
    corpus = load_corpus(tiny)
    agent = AgentEntry(name="bad", command="echo not_json_at_all")
    record, dest = run_corpus(corpus, agent, "m", "fixture", tmp_path / "r", {})
    parse_errs = [c for c in record.cases if c.verdict == "parse_error"]
    assert len(parse_errs) == 2
    assert dest.exists()


# AC-8
def test_ac_8_missing_corpus() -> None:
    from mqo_eval.corpus import load_corpus
    with pytest.raises(FileNotFoundError):
        load_corpus("/tmp/no_such_corpus_xyzzy_99999.yaml")


# AC-9
def test_ac_9_summary_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mqo_eval.corpus import load_corpus
    from mqo_eval.registry import load_registry, resolve_agent
    from mqo_eval.runner import run_corpus
    corpus = load_corpus(CORPUS)
    registry = load_registry(REPO_ROOT / "agents.yaml")
    agent = resolve_agent("stub", registry)
    _, dest = run_corpus(corpus, agent, "m", "fixture", tmp_path / "r", {})
    try:
        from mqo_eval.cli import main
        main(["summary", "--results", str(dest)])
    except SystemExit:
        pass
    output = capsys.readouterr().out
    assert "/" in output  # pass rate N/M


# ── Corpus equivalent_attributes + equivalent_values round-trip ───────────────


def test_corpus_equivalent_attributes_loaded() -> None:
    """equivalent_attributes from YAML are parsed into Query.equivalent_attributes."""
    from mqo_eval.corpus import load_corpus
    corpus = load_corpus(CORPUS)
    # The 3 failing cases should have non-empty equivalent_attributes declared.
    cases_with_equiv = {
        q.id: q.equivalent_attributes
        for q in corpus.queries
        if q.equivalent_attributes
    }
    # At minimum the three recovering cases must have declarations.
    assert "avg-sales-quantity-per-store" in cases_with_equiv, (
        "avg-sales-quantity-per-store must have equivalent_attributes declared"
    )
    assert "total-net-profit-per-product" in cases_with_equiv, (
        "total-net-profit-per-product must have equivalent_attributes declared"
    )
    assert "customers-ese-store-2001" in cases_with_equiv, (
        "customers-ese-store-2001 must have equivalent_attributes declared"
    )
    # Each declared group must be a non-empty list-of-list.
    for case_id, groups in cases_with_equiv.items():
        assert isinstance(groups, list), f"{case_id}: equivalent_attributes must be a list"
        for g in groups:
            assert isinstance(g, list) and len(g) >= 2, (
                f"{case_id}: each group must have ≥2 names"
            )


def test_corpus_equivalent_values_roundtrip(tmp_path: Path) -> None:
    """equivalent_values from YAML are parsed into Query.equivalent_values."""
    from mqo_eval.corpus import load_corpus
    corpus_path = tmp_path / "test_ev.yaml"
    # Use quoted strings to prevent YAML auto-parsing dates/timestamps.
    corpus_path.write_text(
        'context: ""\n'
        'queries:\n'
        '  - id: q1\n'
        '    nl_query: test\n'
        '    expected_sql: "SELECT 1"\n'
        '    equivalent_values:\n'
        '      September:\n'
        '        - "Sep"\n'
        '        - "09"\n'
        '  - id: q2\n'
        '    nl_query: test2\n'
        '    expected_sql: "SELECT 2"\n'
    )
    corpus = load_corpus(corpus_path)
    q1 = next(q for q in corpus.queries if q.id == "q1")
    assert q1.equivalent_values == {"September": ["Sep", "09"]}
    q2 = next(q for q in corpus.queries if q.id == "q2")
    assert q2.equivalent_values == {}  # not declared → empty
