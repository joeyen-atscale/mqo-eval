"""mqo-eval CLI entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_run(args: argparse.Namespace) -> int:
    from .corpus import load_corpus
    from .registry import RegistryError, load_registry, resolve_agent
    from .runner import run_corpus

    try:
        corpus = load_corpus(args.corpus)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    registry_path = Path(args.agents_yaml)
    registry = load_registry(registry_path)
    try:
        agent_entry = resolve_agent(args.agent, registry)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    results_dir = Path(args.results_dir)
    config = {
        "agent": args.agent,
        "model": args.model,
        "server": args.server,
        "corpus": str(args.corpus),
    }

    if corpus.active:
        print(
            f"running {len(corpus.active)} active cases "
            f"({len(corpus.skipped)} skipped) via agent '{agent_entry.name}'"
        )
    else:
        print("no active cases (all disabled)")

    record, dest = run_corpus(
        corpus=corpus,
        agent_entry=agent_entry,
        model=args.model,
        server=args.server,
        results_dir=results_dir,
        config=config,
    )

    s = record.summary
    if s.active == 0:
        print("summary: no active cases")
    else:
        print(
            f"summary: {s.correct}/{s.active} correct "
            f"({s.accuracy:.0%}) | wrong={s.wrong} no_bind={s.no_bind} "
            f"parse_errors={s.parse_errors} skipped={s.skipped}"
        )
    print(f"record: {dest}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    path = Path(args.results)
    if not path.exists():
        print(f"error: results file not found: {path}", file=sys.stderr)
        return 1
    data = json.loads(path.read_text())
    summary = data.get("summary", {})
    active = summary.get("active", 0)
    if active == 0:
        print("no active cases")
        return 0
    pct = f"{summary.get('accuracy', 0):.0%}"
    print(f"pass rate:   {summary.get('correct', 0)}/{active} ({pct})")
    print(f"wrong:       {summary.get('wrong', 0)}")
    print(f"no_bind:     {summary.get('no_bind', 0)}")
    print(f"parse_error: {summary.get('parse_errors', 0)}")
    print(f"skipped:     {summary.get('skipped', 0)}")
    recall = summary.get("mean_row_recall")
    if recall is not None:
        print(f"mean_recall: {recall:.3f}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mqo-eval", description="MQO eval harness")
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run a corpus through an agent")
    run_p.add_argument("--corpus", required=True, help="path to corpus YAML")
    run_p.add_argument("--agent", default="stub", help="agent name or path")
    run_p.add_argument(
        "--model", default="tpcds_benchmark_model", help="model coordinate"
    )
    run_p.add_argument("--server", default="fixture", help="server label")
    run_p.add_argument("--results-dir", default="results", help="archive root")
    run_p.add_argument("--agents-yaml", default="agents.yaml", help="registry file")

    sum_p = sub.add_parser("summary", help="summarise a run record")
    sum_p.add_argument("--results", required=True, help="path to RunRecord JSON")

    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        sys.exit(_cmd_run(args))
    elif args.command == "summary":
        sys.exit(_cmd_summary(args))
