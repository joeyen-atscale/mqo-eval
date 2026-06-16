//! Acceptance tests for the RunRecord archive (PRD-mqoeval-run-record-archive).
//!
//! Covers:
//!  - RunRecord serialises and round-trips (NFR1)
//!  - Two cases, optional metric fields absent → null/omitted (FR3 / AC4)
//!  - Archive file is written to the correct path tree (FR4 / AC1-AC2)
//!  - A second run produces a SECOND distinct file, not an overwrite (FR4 / AC3)
//!  - `summarize_file` works on both a RunRecord file and a legacy flat array (FR6 / AC5)

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::doc_markdown,
    clippy::missing_panics_doc,
    clippy::indexing_slicing,
    clippy::uninlined_format_args,
    clippy::redundant_clone,
    clippy::redundant_closure_for_method_calls,
)]

use mqo_eval::{
    run::{run, OutputFormat, RunConfig},
    summary::summarize_file,
    types::{CaseRecord, OracleMode, RunRecord, ServerInfo, RunConfigRecord, SummaryStats, Verdict},
};

// ────────────────────────────────────────────────────────────────────────────
// Helpers shared across sub-tests
// ────────────────────────────────────────────────────────────────────────────

fn write_stub_agent(dir: &tempfile::TempDir, answer: &str) -> String {
    use std::os::unix::fs::PermissionsExt;
    let path = dir.path().join(format!("stub_{answer}.sh"));
    let script = format!(
        "#!/bin/sh\necho '{{\"bound_mqo\":\"stub\",\"result_rows\":[[\"{answer}\"]],\"confidence\":0.9,\"pillars_fired\":[\"bind\"]}}'\n",
        answer = answer
    );
    std::fs::write(&path, script).expect("write stub script");
    let mut perms = std::fs::metadata(&path).expect("metadata").permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&path, perms).expect("set permissions");
    path.to_string_lossy().into_owned()
}

fn write_questions_yaml(tmp: &tempfile::TempDir, id_prefix: &str, expected: &str) -> String {
    let path = tmp.path().join(format!("{id_prefix}_questions.yaml"));
    let yaml = format!(
        "questions:\n  - id: {id_prefix}q1\n    text: 'What is the answer?'\n    expected_answer: '{expected}'\n    model: tb/sales\n  - id: {id_prefix}q2\n    text: 'Second question?'\n    expected_answer: '{expected}'\n    model: tb/sales\n"
    );
    std::fs::write(&path, yaml).expect("write questions yaml");
    path.to_string_lossy().into_owned()
}

// ────────────────────────────────────────────────────────────────────────────
// Unit test: RunRecord with two cases round-trips through JSON
// ────────────────────────────────────────────────────────────────────────────

#[test]
fn run_record_roundtrip() {
    let record = RunRecord {
        run_id: "20260616T120000Z-testcorp".to_owned(),
        started_at: "2026-06-16T12:00:00Z".to_owned(),
        finished_at: "2026-06-16T12:00:05Z".to_owned(),
        agent: "mqo-agent".to_owned(),
        server: ServerInfo {
            name: "fixture".to_owned(),
            mqo_mcp_version: None,
        },
        corpus_id: "test_corpus".to_owned(),
        config: RunConfigRecord {
            repeat: 1,
            min_pass_reps: 1,
            pass_threshold: 1.0,
            catalog: None,
            model: "tb/sales".to_owned(),
            oracle_mode: "fixture".to_owned(),
            max_result_rows: 1000,
        },
        cases: vec![
            CaseRecord {
                id: "q1".to_owned(),
                verdict: Verdict::Correct,
                latency_ms: 42,
                row_recall: None,
                column_recall: None,
                jaccard: None,
                rep_verdicts: None,
                detail: None,
            },
            CaseRecord {
                id: "q2".to_owned(),
                verdict: Verdict::Wrong,
                latency_ms: 77,
                row_recall: None,
                column_recall: None,
                jaccard: None,
                rep_verdicts: None,
                detail: None,
            },
        ],
        summary: SummaryStats {
            accuracy: 0.5,
            no_bind_rate: 0.0,
            mean_confidence: 0.0,
            mean_latency_ms: 59.5,
            total: 2,
            correct: 1,
            wrong: 1,
            no_bind: 0,
        },
    };

    let json = serde_json::to_string_pretty(&record).expect("serialise RunRecord");
    let back: RunRecord = serde_json::from_str(&json).expect("deserialise RunRecord");

    assert_eq!(back.run_id, record.run_id);
    assert_eq!(back.cases.len(), 2);
    assert_eq!(back.cases[0].verdict, Verdict::Correct);
    assert_eq!(back.cases[1].verdict, Verdict::Wrong);
    assert_eq!(back.summary.total, 2);

    // Optional metric fields must be absent from the JSON (not serialised as 0 or null).
    assert!(
        !json.contains("\"row_recall\""),
        "row_recall must be omitted when None, not written as null"
    );
    assert!(
        !json.contains("\"jaccard\""),
        "jaccard must be omitted when None"
    );
}

// ────────────────────────────────────────────────────────────────────────────
// summarize_file: accepts RunRecord shape
// ────────────────────────────────────────────────────────────────────────────

#[test]
fn summarize_file_accepts_run_record() {
    let tmp = tempfile::tempdir().expect("tempdir");

    // Write a RunRecord with 2 cases.
    let record = RunRecord {
        run_id: "20260616T130000Z-srtest00".to_owned(),
        started_at: "2026-06-16T13:00:00Z".to_owned(),
        finished_at: "2026-06-16T13:00:01Z".to_owned(),
        agent: "stub".to_owned(),
        server: ServerInfo { name: "fixture".to_owned(), mqo_mcp_version: None },
        corpus_id: "srtest".to_owned(),
        config: RunConfigRecord {
            repeat: 1,
            min_pass_reps: 1,
            pass_threshold: 1.0,
            catalog: None,
            model: "tb/m".to_owned(),
            oracle_mode: "fixture".to_owned(),
            max_result_rows: 1000,
        },
        cases: vec![
            CaseRecord {
                id: "q1".to_owned(),
                verdict: Verdict::Correct,
                latency_ms: 10,
                row_recall: None,
                column_recall: None,
                jaccard: None,
                rep_verdicts: None,
                detail: None,
            },
            CaseRecord {
                id: "q2".to_owned(),
                verdict: Verdict::NoBind,
                latency_ms: 5,
                row_recall: None,
                column_recall: None,
                jaccard: None,
                rep_verdicts: None,
                detail: None,
            },
        ],
        summary: SummaryStats {
            accuracy: 0.5,
            no_bind_rate: 0.5,
            mean_confidence: 0.0,
            mean_latency_ms: 7.5,
            total: 2,
            correct: 1,
            wrong: 0,
            no_bind: 1,
        },
    };

    let record_path = tmp.path().join("run_record.json");
    let json = serde_json::to_string_pretty(&record).unwrap();
    std::fs::write(&record_path, &json).unwrap();

    let stats = summarize_file(record_path.to_str().unwrap()).expect("summarize_file on RunRecord");
    assert_eq!(stats.total, 2);
    assert_eq!(stats.correct, 1);
    assert_eq!(stats.no_bind, 1);
}

// ────────────────────────────────────────────────────────────────────────────
// summarize_file: accepts legacy flat array
// ────────────────────────────────────────────────────────────────────────────

#[test]
fn summarize_file_accepts_legacy_flat_array() {
    use mqo_eval::types::QuestionResult;

    let tmp = tempfile::tempdir().expect("tempdir");
    let results = vec![
        QuestionResult {
            question: "Q1".to_owned(),
            id: "q1".to_owned(),
            verdict: Verdict::Correct,
            confidence: 0.9,
            pillars_fired: vec![],
            latency_ms: 50,
            oracle_outcome: None,
        },
        QuestionResult {
            question: "Q2".to_owned(),
            id: "q2".to_owned(),
            verdict: Verdict::Wrong,
            confidence: 0.3,
            pillars_fired: vec![],
            latency_ms: 30,
            oracle_outcome: None,
        },
    ];

    let flat_path = tmp.path().join("legacy.json");
    std::fs::write(&flat_path, serde_json::to_string_pretty(&results).unwrap()).unwrap();

    let stats = summarize_file(flat_path.to_str().unwrap())
        .expect("summarize_file on legacy flat array");
    assert_eq!(stats.total, 2);
    assert_eq!(stats.correct, 1);
    assert_eq!(stats.wrong, 1);
}

// ────────────────────────────────────────────────────────────────────────────
// Archive path: run writes file to results/<agent>/<server>/<corpus>/<id>.json
// ────────────────────────────────────────────────────────────────────────────

#[test]
fn archive_file_at_expected_path() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let results_dir = tmp.path().join("results");
    let agent_cmd = write_stub_agent(&tmp, "42");
    let questions_path = write_questions_yaml(&tmp, "ac_path", "42");
    let out_path = tmp.path().join("flat.json");

    let cfg = RunConfig {
        questions_path: questions_path.clone(),
        model: "tb/sales".to_owned(),
        agent_cmd: agent_cmd.clone(),
        oracle: OracleMode::Fixture,
        pg_host: None,
        pg_pass_env: "ATSCALE_PG_PASS".to_owned(),
        catalog_name: "atscale_catalogs".to_owned(),
        model_name: "tpcds_benchmark_model".to_owned(),
        max_result_rows: 50_000,
        pg_user: "atscale".to_owned(),
        out_path: out_path.to_string_lossy().into_owned(),
        format: OutputFormat::Text,
        results_dir: results_dir.to_string_lossy().into_owned(),
    };

    run(&cfg).expect("run should succeed");

    // Derive the expected archive sub-path.
    let agent_basename = std::path::Path::new(&agent_cmd)
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let server = "fixture";
    let corpus_id = "ac_path_questions";

    let archive_dir = results_dir.join(&agent_basename).join(server).join(corpus_id);
    let entries: Vec<_> = std::fs::read_dir(&archive_dir)
        .expect("archive directory must exist")
        .filter_map(|e| e.ok())
        .collect();

    assert_eq!(entries.len(), 1, "exactly one archive file must be written");
    let archive_file = &entries[0].path();
    assert!(
        archive_file.extension().and_then(|e| e.to_str()) == Some("json"),
        "archive file must be a .json file"
    );

    // File must parse as a valid RunRecord.
    let content = std::fs::read_to_string(archive_file).unwrap();
    let record: RunRecord = serde_json::from_str(&content).expect("archive must be a valid RunRecord");
    assert_eq!(record.corpus_id, corpus_id);
    assert_eq!(record.cases.len(), 2);
    assert!(!record.run_id.is_empty());
}

// ────────────────────────────────────────────────────────────────────────────
// Two runs → two distinct archive files (no overwrite)
// ────────────────────────────────────────────────────────────────────────────

#[test]
fn two_runs_produce_two_archive_files() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let results_dir = tmp.path().join("results");
    let agent_cmd = write_stub_agent(&tmp, "99");
    let questions_path = write_questions_yaml(&tmp, "two_runs", "99");

    // Run 1.
    let cfg1 = RunConfig {
        questions_path: questions_path.clone(),
        model: "tb/sales".to_owned(),
        agent_cmd: agent_cmd.clone(),
        oracle: OracleMode::Fixture,
        pg_host: None,
        pg_pass_env: "ATSCALE_PG_PASS".to_owned(),
        catalog_name: "atscale_catalogs".to_owned(),
        model_name: "tpcds_benchmark_model".to_owned(),
        max_result_rows: 50_000,
        pg_user: "atscale".to_owned(),
        out_path: tmp.path().join("flat1.json").to_string_lossy().into_owned(),
        format: OutputFormat::Text,
        results_dir: results_dir.to_string_lossy().into_owned(),
    };
    run(&cfg1).expect("run 1 should succeed");

    // Small sleep to ensure the timestamp differs by at least 1 second.
    std::thread::sleep(std::time::Duration::from_secs(1));

    // Run 2 — same config, different out_path.
    let cfg2 = RunConfig {
        questions_path: questions_path.clone(),
        model: "tb/sales".to_owned(),
        agent_cmd: agent_cmd.clone(),
        oracle: OracleMode::Fixture,
        pg_host: None,
        pg_pass_env: "ATSCALE_PG_PASS".to_owned(),
        catalog_name: "atscale_catalogs".to_owned(),
        model_name: "tpcds_benchmark_model".to_owned(),
        max_result_rows: 50_000,
        pg_user: "atscale".to_owned(),
        out_path: tmp.path().join("flat2.json").to_string_lossy().into_owned(),
        format: OutputFormat::Text,
        results_dir: results_dir.to_string_lossy().into_owned(),
    };
    run(&cfg2).expect("run 2 should succeed");

    let agent_basename = std::path::Path::new(&agent_cmd)
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let archive_dir = results_dir
        .join(&agent_basename)
        .join("fixture")
        .join("two_runs_questions");

    let file_count = std::fs::read_dir(&archive_dir)
        .expect("archive dir must exist")
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().and_then(|x| x.to_str()) == Some("json"))
        .count();

    assert_eq!(
        file_count, 2,
        "two runs must produce two distinct archive files, got {file_count}"
    );
}
