"""Tests for the record-replay cassette agent.

All tests are mocked/in-memory — no real subprocesses or API calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from mqo_eval.agents.replay_agent import (
    CASSETTE_SCHEMA_VERSION,
    CassetteEntry,
    CassetteStore,
    RecordAgent,
    ReplayAgent,
)

# ---------------------------------------------------------------------------
# 1. CassetteEntry roundtrip
# ---------------------------------------------------------------------------


def test_cassette_entry_roundtrip() -> None:
    entry = CassetteEntry(
        case_id="q01",
        model="tpcds_benchmark_model",
        corpus_id="tpcds_sql_derived",
        answer_json='{"answer_type": "scalar", "value": 42}',
    )
    d = entry.to_dict()
    restored = CassetteEntry.from_dict(d)

    assert restored.case_id == entry.case_id
    assert restored.model == entry.model
    assert restored.corpus_id == entry.corpus_id
    assert restored.answer_json == entry.answer_json
    assert restored.schema_version == CASSETTE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. CassetteStore append + load
# ---------------------------------------------------------------------------


def test_cassette_store_append_and_load(tmp_path: Path) -> None:
    cassette_path = tmp_path / "test.jsonl"
    store = CassetteStore(cassette_path)

    entry1 = CassetteEntry(
        case_id="q01",
        model="model_a",
        corpus_id="corpus_x",
        answer_json='{"answer_type": "scalar", "value": 1}',
    )
    entry2 = CassetteEntry(
        case_id="q02",
        model="model_b",
        corpus_id="corpus_y",
        answer_json='{"answer_type": "scalar", "value": 2}',
    )

    store.append(entry1)
    store.append(entry2)

    loaded = store.load()
    assert len(loaded) == 2
    assert "q01" in loaded
    assert "q02" in loaded
    assert loaded["q01"].answer_json == entry1.answer_json
    assert loaded["q02"].model == entry2.model


# ---------------------------------------------------------------------------
# 3. Version mismatch raises ValueError
# ---------------------------------------------------------------------------


def test_version_mismatch_raises() -> None:
    bad_dict = {
        "schema_version": "99",
        "case_id": "q01",
        "model": "m",
        "corpus_id": "c",
        "answer_json": "{}",
    }
    with pytest.raises(ValueError, match="incompatible cassette version"):
        CassetteEntry.from_dict(bad_dict)


# ---------------------------------------------------------------------------
# 4. ReplayAgent — known case returns recorded answer
# ---------------------------------------------------------------------------


def test_replay_known_case(tmp_path: Path) -> None:
    cassette_path = tmp_path / "cassette.jsonl"
    store = CassetteStore(cassette_path)
    expected_answer = '{"answer_type": "scalar", "value": 99}'
    store.append(
        CassetteEntry(
            case_id="q05",
            model="model_x",
            corpus_id="corpus_z",
            answer_json=expected_answer,
        )
    )

    agent = ReplayAgent(store)
    result = agent.answer("q05")
    assert result == expected_answer


# ---------------------------------------------------------------------------
# 5. ReplayAgent miss — non-strict → cannot_answer
# ---------------------------------------------------------------------------


def test_replay_miss_non_strict(tmp_path: Path) -> None:
    cassette_path = tmp_path / "cassette.jsonl"
    store = CassetteStore(cassette_path)
    # Write a different case so the file exists but misses our target
    store.append(
        CassetteEntry(
            case_id="q01",
            model="m",
            corpus_id="c",
            answer_json='{"answer_type": "scalar", "value": 0}',
        )
    )

    agent = ReplayAgent(store, strict=False)
    result_str = agent.answer("q_missing")
    result = json.loads(result_str)
    assert result["answer_type"] == "cannot_answer"
    assert "cassette-miss" in result["reason"]


# ---------------------------------------------------------------------------
# 6. ReplayAgent miss — strict=True raises KeyError
# ---------------------------------------------------------------------------


def test_replay_miss_strict(tmp_path: Path) -> None:
    cassette_path = tmp_path / "cassette.jsonl"
    store = CassetteStore(cassette_path)
    store.append(
        CassetteEntry(
            case_id="q01",
            model="m",
            corpus_id="c",
            answer_json='{"answer_type": "scalar", "value": 0}',
        )
    )

    agent = ReplayAgent(store, strict=True)
    with pytest.raises(KeyError, match="cassette-miss"):
        agent.answer("q_missing")


# ---------------------------------------------------------------------------
# 7. RecordAgent calls delegate subprocess and records stdout
# ---------------------------------------------------------------------------


def test_record_calls_delegate(tmp_path: Path) -> None:
    cassette_path = tmp_path / "cassette.jsonl"
    store = CassetteStore(cassette_path)

    fake_answer = '{"answer_type": "scalar", "value": 7}'
    mock_result = MagicMock()
    mock_result.stdout = fake_answer + "\n"

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        agent = RecordAgent("some_delegate_cmd", store)
        env_case = {"case_id": "q10", "model": "model_m", "corpus_id": "corpus_c"}
        returned = agent.answer("q10", env_case)

    # subprocess.run was called once
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args

    # The command was passed
    assert "some_delegate_cmd" in call_kwargs[0][0]

    # The returned answer is the stripped stdout
    assert returned == fake_answer

    # The cassette was written
    loaded = store.load()
    assert "q10" in loaded
    assert loaded["q10"].answer_json == fake_answer
    assert loaded["q10"].model == "model_m"
    assert loaded["q10"].corpus_id == "corpus_c"


# ---------------------------------------------------------------------------
# 8. No API/subprocess calls during replay
# ---------------------------------------------------------------------------


def test_no_api_calls_during_replay(tmp_path: Path) -> None:
    cassette_path = tmp_path / "cassette.jsonl"
    store = CassetteStore(cassette_path)
    store.append(
        CassetteEntry(
            case_id="q01",
            model="m",
            corpus_id="c",
            answer_json='{"answer_type": "scalar", "value": 1}',
        )
    )

    agent = ReplayAgent(store)

    with patch("subprocess.run") as mock_run:
        result = agent.answer("q01")

    # No subprocess calls should have been made
    mock_run.assert_not_called()
    assert result == '{"answer_type": "scalar", "value": 1}'


# ---------------------------------------------------------------------------
# 9. agents.yaml contains a `replay` entry
# ---------------------------------------------------------------------------


def test_replay_agent_registered_in_yaml() -> None:
    agents_yaml_path = Path(__file__).parent.parent / "agents.yaml"
    assert agents_yaml_path.exists(), f"agents.yaml not found at {agents_yaml_path}"

    with agents_yaml_path.open() as f:
        config = yaml.safe_load(f)

    agents = config.get("agents", {})
    assert "replay" in agents, (
        f"'replay' not found in agents.yaml; got: {list(agents.keys())}"
    )

    replay_cfg = agents["replay"]
    assert replay_cfg.get("capabilities", {}).get("requires_api") is False, (
        "replay agent must have requires_api: false"
    )
