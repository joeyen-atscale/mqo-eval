"""Record-replay (cassette) agent — zero-API CI gate.

Usage:
  # Record (wraps a delegate):
  python -m mqo_eval.agents.replay_agent record \
      --delegate stub --cassette cassettes/run.jsonl

  # Replay:
  python -m mqo_eval.agents.replay_agent replay --cassette cassettes/run.jsonl
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CASSETTE_SCHEMA_VERSION = "1"


@dataclass
class CassetteEntry:
    case_id: str
    model: str  # e.g. "tpcds_benchmark_model"
    corpus_id: str  # stem of corpus file
    answer_json: str  # the raw AgentAnswer JSON
    schema_version: str = CASSETTE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "model": self.model,
            "corpus_id": self.corpus_id,
            "answer_json": self.answer_json,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "CassetteEntry":
        if d.get("schema_version") != CASSETTE_SCHEMA_VERSION:
            raise ValueError(
                f"incompatible cassette version {d.get('schema_version')!r}; "
                f"expected {CASSETTE_SCHEMA_VERSION!r}"
            )
        return cls(
            case_id=d["case_id"],
            model=d["model"],
            corpus_id=d["corpus_id"],
            answer_json=d["answer_json"],
            schema_version=d["schema_version"],
        )


class CassetteStore:
    """Read/write cassette JSONL files."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: CassetteEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def load(self) -> dict[str, CassetteEntry]:
        """Load all entries keyed by case_id. Raises FileNotFoundError if absent."""
        entries: dict[str, CassetteEntry] = {}
        for line in self.path.read_text().splitlines():
            e = CassetteEntry.from_dict(json.loads(line))
            entries[e.case_id] = e
        return entries


class RecordAgent:
    """Wraps a delegate agent; records each answer to a cassette."""

    def __init__(self, delegate_command: str, store: CassetteStore) -> None:
        self.delegate_command = delegate_command
        self.store = store

    def answer(self, case_id: str, env_case: dict[str, str]) -> str:
        """Run delegate, record its stdout, return it."""
        env = os.environ.copy()
        env["MQO_EVAL_CASE"] = json.dumps(env_case)
        result = subprocess.run(
            self.delegate_command, shell=True, capture_output=True, text=True, env=env
        )
        answer_json = result.stdout.strip()
        self.store.append(
            CassetteEntry(
                case_id=case_id,
                model=env_case.get("model", ""),
                corpus_id=env_case.get("corpus_id", ""),
                answer_json=answer_json,
            )
        )
        print(answer_json)
        return answer_json


class ReplayAgent:
    """Serves recorded answers from a cassette — zero API/model calls."""

    def __init__(self, store: CassetteStore, strict: bool = False) -> None:
        self.store = store
        self.strict = strict
        self._entries: dict[str, CassetteEntry] | None = None

    def _load(self) -> dict[str, CassetteEntry]:
        if self._entries is None:
            self._entries = self.store.load()
        return self._entries

    def answer(self, case_id: str) -> str:
        entries = self._load()
        if case_id not in entries:
            if self.strict:
                raise KeyError(f"cassette-miss: case_id {case_id!r} not in cassette")
            return json.dumps(
                {"answer_type": "cannot_answer", "reason": f"cassette-miss:{case_id}"}
            )
        return entries[case_id].answer_json


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="replay_agent")
    sub = p.add_subparsers(dest="mode", required=True)

    rec = sub.add_parser("record", help="wrap a delegate and record answers")
    rec.add_argument("--delegate", required=True, help="delegate agent command")
    rec.add_argument("--cassette", required=True, help="output cassette JSONL path")
    rec.add_argument("--case-id", required=True)
    rec.add_argument("--model", default="")
    rec.add_argument("--corpus-id", default="")

    rep = sub.add_parser("replay", help="replay answers from a cassette")
    rep.add_argument("--cassette", required=True, help="cassette JSONL path")
    rep.add_argument("--case-id", required=True)
    rep.add_argument("--strict", action="store_true")

    args = p.parse_args()

    if args.mode == "record":
        rec_store = CassetteStore(Path(args.cassette))
        rec_agent = RecordAgent(args.delegate, rec_store)
        env_case: dict[str, str] = json.loads(os.environ.get("MQO_EVAL_CASE", "{}"))
        env_case.setdefault("case_id", args.case_id)
        env_case.setdefault("model", args.model)
        env_case.setdefault("corpus_id", args.corpus_id)
        rec_agent.answer(args.case_id, env_case)

    elif args.mode == "replay":
        rep_store = CassetteStore(Path(args.cassette))
        rep_agent = ReplayAgent(rep_store, strict=args.strict)
        print(rep_agent.answer(args.case_id))


if __name__ == "__main__":
    main()
