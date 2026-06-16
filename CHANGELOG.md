# Changelog

## v0.3.0 — 2026-06-16

Rebase-resolved PGWire golden-SQL oracle branch onto main (run-record-archive). Merged RunConfig fields (catalog_name, model_name, pg_user from branch; results_dir from HEAD; dropped redundant catalog:Option<String>), made run() async-aware via Tokio runtime, added run_sql_corpus path for PR#42 SqlQueriesFile corpus shape, kept CaseRecord generation for RunRecord archive on both code paths. Fixed post-rebase compile errors (oracle_outcome missing in summary.rs and acceptance_archive.rs; large_enum_variant on Command enum). Clippy -D warnings clean, 36/36 tests green.

## v0.2.0 — 2026-06-16

run-record-archive: extend RunRecord archive
