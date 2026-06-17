# Changelog

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
