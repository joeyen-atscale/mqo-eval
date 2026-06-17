# Changelog

## v0.2.0 — pgwire-golden-oracle
PGWire golden-SQL oracle: execute `expected_sql` over a direct PGWire connection
to produce a typed `ReferenceTable`, `Oversize`, or `OracleError`. Mocked tests;
no live DB required for CI.

## v0.1.0 — harness-core
Initial Python eval harness: PR #42 corpus loader, AgentAnswer contract
(tabular/handle/scalar/cannot_answer + JSON Schema), --agent registry,
run loop, RunRecord archive, CLI (run/summary), stub agent.
