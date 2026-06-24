# Changelog

## v0.8.1 — trace-observability
Surfaces the mqo-mcp-server v0.56.0 result-envelope signals in `--trace`: `_extract_bound_sql` now reads `compiled_query`/`compiled_dax`/`compiled_mdx` (the keys the server actually emits) so compiled SQL/DAX lights up per case, and a new `_extract_signals` pulls `backend`, `routing_reason`, `row_count`, `blank_member_rows`, `notes[]`, and `handle` as first-class trace fields rendered by `mqo-eval trace`. Corpus-shape tests updated for the second disabled BUG-2 case (`customer-details-new-jersey`): now 22 total / 20 active / 2 skipped. README quickstart output and test count (172) corrected. All 172 tests green.

## v0.8.0 — gold-via-cli
Adds `--oracle cli` backend: shells out to `mqo-pg-query` (Rust CLI with OIDC/TLS auth) to mint gold `ReferenceTable` per case. New `src/mqo_eval/oracle_cli.py` with `CliOracleConfig`, `execute_golden_cli`, and `cli_precheck` (eager SELECT-1 pre-check fails fast <10s if binary is missing/misconfigured). Wired into `runner.py` and `cli.py` alongside existing `fixture`/`pgwire` oracles; adds `--gold-query-cmd` and `--cli-endpoint` flags. 23 new mocked tests; all 108 tests green; ruff+mypy clean.

## v0.7.0 — claude-oauth-agent
Headless Claude agent via OAuth subscription: no API key, no local model. Invokes claude -p --output-format json with mqo-mcp wired via --mcp-config, tools pre-approved via --allowedTools mcp__mqo__*. ANTHROPIC_API_KEY stripped from child env. 9 mocked tests.


## v0.6.0 — runner-integration
Wires oracle.execute_golden + scoring.score_case into run_corpus; replaces the _verdict_from_answer stub. Adds k-of-n (--repeat/--min-pass-reps), --pass-threshold, --oracle pgwire path with PGWire precheck. Mean recall/Jaccard in summary. 12 new integration tests.


## v0.5.0 — record-replay-agent
Cassette agent: record mode wraps any delegate agent and saves AgentAnswer envelopes to JSONL; replay mode serves them back with zero API/model calls. Schema-versioned cassettes. CI gate: replay runs entire corpus deterministically.


## v0.4.0 — handle-scoring
Recall/Jaccard scoring engine: cell canonicalization, column normalization + equivalence groups, multiset row matching, scalar 1×1 fast-path, typed verdicts (correct/wrong/oversize/no_bind). 18 tests.


## v0.3.0 — api-free-agent
OpenAI-compatible NL→query agent: session-holding MCP stdio transport, handle
detection, turn cap, no-Anthropic-required; all 8 tests mocked.

## v0.2.0 — pgwire-golden-oracle
## v0.3.0 — api-free-agent
OpenAI-compatible NL→query agent: session-holding MCP stdio transport, handle
detection, turn cap, no-Anthropic-required; all 8 tests mocked.
PGWire golden-SQL oracle: execute `expected_sql` over a direct PGWire connection
to produce a typed `ReferenceTable`, `Oversize`, or `OracleError`. Mocked tests;
no live DB required for CI.

## v0.1.0 — harness-core
Initial Python eval harness: PR #42 corpus loader, AgentAnswer contract
(tabular/handle/scalar/cannot_answer + JSON Schema), --agent registry,
run loop, RunRecord archive, CLI (run/summary), stub agent.
