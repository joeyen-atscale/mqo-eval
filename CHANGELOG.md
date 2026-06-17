# Changelog

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
