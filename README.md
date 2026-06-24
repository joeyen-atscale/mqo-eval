# mqo-eval

An eval harness that scores a natural-language agent against an AtScale semantic layer by checking the result set it returns, not the query it wrote.

## Why it exists

Two queries can be written differently and return the same answer; two queries can look almost identical and return different answers. Grading an agent on the SQL it emits measures the wrong thing. What matters is whether the rows it brought back match the truth.

So `mqo-eval` grades on the result set. For each case it runs the reference SQL through an oracle to mint the ground-truth table, asks the agent the question in plain language, and compares the two tables by recall and Jaccard overlap. The agent's path to the answer — which protocol it chose, how it phrased the query, whether it returned rows inline or a dataset handle — is its own business. Only the answer is scored.

The harness is API-free at its core: a corpus loader, a typed answer contract, the scoring engine, and a record/replay cassette all run with no model and no network. That keeps the test of the *scorer* separate from the cost and variance of the *agent*.

## Install

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/joeyen-atscale/mqo-eval
cd mqo-eval
uv sync
```

That installs the `mqo-eval` CLI and a dev group (ruff, mypy, pytest). Run anything below with `uv run`.

## Quickstart

Run the bundled corpus through the built-in stub agent against the offline oracle:

```bash
uv run mqo-eval run \
  --corpus corpus/tpcds_sql_derived_limited.yaml \
  --agent stub \
  --oracle fixture
```

```
running 20 active cases (2 skipped) via agent 'stub'
summary: pass rate: 0/20 (0%) | wrong=0 no_bind=20 parse_errors=0 skipped=2
record: results/stub/fixture/tpcds_sql_derived_limited/<timestamp>-tpcds_sq.json
```

The stub answers `cannot_answer` to every question, so 0/20 is the expected result — this run proves the loop, the record archive, and the summary path work end to end, with no API key and no database. Each run writes a JSON `RunRecord` under `results/<agent>/<server>/<corpus>/`; replay it later:

```bash
uv run mqo-eval summary --results results/stub/fixture/tpcds_sql_derived_limited/<timestamp>-tpcds_sq.json
```

To grade a real agent you need an oracle that can produce ground truth and an agent that can answer. See **Oracles** and **Agents** below.

## How it works

Four pieces, each independently testable:

- **Corpus** — a YAML file of cases. Each case carries an `id`, the `nl_query` (what the agent is asked), and the `expected_sql` (the reference query the oracle runs). Cases can be `disabled`, and can declare `equivalent_attributes` / `equivalent_values` so a correct answer expressed differently isn't counted as a miss. `corpus/tpcds_sql_derived_limited.yaml` ships 20 active TPC-DS cases.
- **Agent** — any subprocess that reads the case from the `MQO_EVAL_CASE` env var and prints one JSON answer to stdout. The answer is a typed envelope: `tabular` (columns + rows), `scalar`, `handle` (a dataset handle to resolve), or `cannot_answer`. The contract and its JSON Schema live in `src/mqo_eval/contract.py` and `contract/agent-answer.schema.json`.
- **Oracle** — runs the reference SQL to mint the ground-truth table (see below).
- **Scoring** — canonicalizes cells, normalizes columns, matches rows as a multiset, and reports row recall and Jaccard overlap. A case is `correct` when row recall clears `--pass-threshold` (default 0.95); other verdicts are `wrong`, `no_bind`, `oversize`, and `parse_error`.

Run a case `k` times with `--repeat k` and require `--min-pass-reps` of those reps to pass — the way you separate single-shot luck from capability. `summary` then flags cases whose reps disagreed.

## Oracles

`--oracle` selects how ground truth is produced:

| Mode | What it does |
| --- | --- |
| `fixture` | Offline. No scoring against data — verdicts come from the answer type only. The default; what the quickstart uses. |
| `pgwire` | Runs `expected_sql` over a direct PGWire connection (psycopg2) to the AtScale cluster. |
| `cli` | Shells out to the `mqo-pg-query` binary, which handles OIDC/TLS auth to the cluster. |
| `precomputed` | Reads a pre-minted gold cache JSON (`--gold-file`), so a scored run needs no live database. |

The live oracles read credentials from environment variables — names only, never values, are configurable on the CLI: `ATSCALE_PG_PASS` (password, via `--pg-pass-env`), `ATSCALE_PG_USER`, plus `--pg-host`, `--pg-dbname`, and `--pg-sslmode`. `scripts/mint-gold.py` builds a `precomputed` cache.

## Agents

Agents are registered in `agents.yaml` by name. The bundled ones:

- **`stub`** — answers `cannot_answer` to everything. The self-test backend; no API, no network.
- **`replay`** — serves answers from a recorded cassette, so a whole corpus replays deterministically with zero model calls.
- **`oai-agent`** — an OpenAI-compatible natural-language→query agent over an MCP stdio transport.
- **`claude-oauth`** — headless Claude via an OAuth subscription (`claude -p`), with `mqo-mcp` wired in. No API key; `ANTHROPIC_API_KEY` is stripped from the child environment.

Point `--agent` at any of these, or add your own subprocess to `agents.yaml`.

## Rule-violation harness

A separate, deterministic sub-harness measures per-rule query-syntax violation rates for the AtScale semantic-layer query rules. It does not call an LLM and does not require a live AtScale cluster — it runs SQL-text-only checks against a labeled corpus.

**When to use it:** after migrating a rule from LLM prose to a server validator, to confirm the enforcement gate works and to capture a before/after delta. It is also the CI regression gate that fails the build when a previously-migrated rule's admission rate climbs back above its configured floor.

```bash
# Print a per-rule summary table (never exits 1, for report mode):
uv run python -m mqo_eval.rule_ci_floor --report-only

# Enforce floors (fails with exit 1 on regression):
uv run python -m mqo_eval.rule_ci_floor

# Single rule only:
uv run python -m mqo_eval.rule_ci_floor --rule R-MS

# Emit structured JSON alongside the table:
uv run python -m mqo_eval.rule_ci_floor --json /tmp/rule-report.json --report-only
```

The output columns are:

| Column | Meaning |
| --- | --- |
| `Admiss.Rate` | Fraction of *violating* corpus cases that the checker lets through (lower is better; 0 % = all violations caught). |
| `FP.Rate` | Fraction of *conforming* corpus cases that are incorrectly rejected (lower is better). |
| `Floor` | Configured maximum tolerated admission rate for this rule (per `RULE_FLOORS` in `rule_ci_floor.py`). |
| `Status` | `PASS`, `FAIL (>floor)`, or `N/A (no violating cases)`. |

The corpus lives in `corpus/rule_violations/` (one `cases.yaml` + one `rules.yaml`). Each case declares the rule(s) it exercises, whether it is a `violating` or `conforming` case, and whether it requires LLM judgment (`lm_driven: true`). LLM-driven cases are counted as skipped and never contribute to the hard CI floor, keeping the gate deterministic (NFR3).

The harness also checks for a live eval tick lock before starting and refuses to run concurrently with the MQO-arm eval, preventing shared-API contention (FR7).

## Where it fits

`mqo-eval` is the scoring end of the MQO loop. `mqo-mcp` serves the AtScale semantic layer to an agent over MCP; this harness measures how well that agent answers. The `buildloop/` directory holds the closed-loop driver — run the corpus, digest the result into `ledger.md`, feed the gaps into the next build cycle.

## Status

Version 0.8.1. The core — corpus, contract, scoring, record/replay — is covered by the test suite (`uv run pytest`; 172 tests, all offline and mocked). The live oracles (`pgwire`, `cli`) and the `oai-agent` / `claude-oauth` agents require a configured AtScale cluster and credentials, which CI does not exercise.
