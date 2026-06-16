//! Acceptance tests for the markdown run summary renderer (FR1–FR6, AC1–AC7).

use mqo_eval::{
    report::markdown,
    types::{
        CaseRecord, RunConfigRecord, RunRecord, ServerInfo, SummaryStats, Verdict,
    },
};

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

fn make_config(repeat: u32) -> RunConfigRecord {
    RunConfigRecord {
        repeat,
        min_pass_reps: 1,
        pass_threshold: 1.0,
        catalog: None,
        model: "claude-sonnet-4-5".to_owned(),
        oracle_mode: "fixture".to_owned(),
        max_result_rows: 1000,
    }
}

fn make_summary(cases: &[CaseRecord]) -> SummaryStats {
    let total = cases.len();
    let correct = cases.iter().filter(|c| c.verdict == Verdict::Correct).count();
    let wrong = cases.iter().filter(|c| c.verdict == Verdict::Wrong).count();
    let no_bind = cases.iter().filter(|c| c.verdict == Verdict::NoBind).count();
    #[allow(clippy::cast_precision_loss)]
    let accuracy = if total == 0 { 0.0 } else { correct as f64 / total as f64 };
    #[allow(clippy::cast_precision_loss)]
    let no_bind_rate = if total == 0 { 0.0 } else { no_bind as f64 / total as f64 };
    SummaryStats {
        accuracy,
        no_bind_rate,
        mean_confidence: 0.9,
        mean_latency_ms: 500.0,
        total,
        correct,
        wrong,
        no_bind,
    }
}

fn make_run(cases: Vec<CaseRecord>, repeat: u32, version: Option<&str>) -> RunRecord {
    let summary = make_summary(&cases);
    RunRecord {
        run_id: "20260616-abcd1234".to_owned(),
        started_at: "2026-06-16T10:00:00Z".to_owned(),
        finished_at: "2026-06-16T10:05:00Z".to_owned(),
        agent: "mqo-agent".to_owned(),
        server: ServerInfo {
            name: "pgwire".to_owned(),
            mqo_mcp_version: version.map(str::to_owned),
        },
        corpus_id: "tpcds_qwf20".to_owned(),
        config: make_config(repeat),
        cases,
        summary,
    }
}

fn make_case(id: &str, verdict: Verdict) -> CaseRecord {
    CaseRecord {
        id: id.to_owned(),
        verdict,
        latency_ms: 200,
        row_recall: None,
        column_recall: None,
        jaccard: None,
        rep_verdicts: None,
        detail: None,
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// AC1: valid CommonMark report with title and result line
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn ac1_valid_commonmark_title_and_result() {
    let cases = vec![
        make_case("q01", Verdict::Correct),
        make_case("q02", Verdict::Wrong),
    ];
    let record = make_run(cases, 1, Some("0.37.0"));
    let md = markdown::render(&record);

    // H1 title containing corpus, agent, server name, oracle mode.
    assert!(md.starts_with("# tpcds_qwf20"), "H1 title: {md}");
    assert!(md.contains("mqo-agent"), "agent in title: {md}");
    assert!(md.contains("pgwire"), "server name in title: {md}");
    assert!(md.contains("0.37.0"), "version in title: {md}");
    assert!(md.contains("fixture"), "oracle mode in title: {md}");
    assert!(md.contains("2026-06-16T10:00:00Z"), "timestamp in title: {md}");

    // Result line.
    assert!(md.contains("## Result: 1/2 passed"), "result line: {md}");
}

// ──────────────────────────────────────────────────────────────────────────────
// AC2: handle-graded run — grouped by verdict + metric badges + advisory
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn ac2_handle_graded_verdict_groups_and_metrics() {
    let cases = vec![
        CaseRecord {
            id: "q01".to_owned(),
            verdict: Verdict::Correct,
            latency_ms: 200,
            row_recall: Some(1.0),
            column_recall: None,
            jaccard: Some(1.0),
            rep_verdicts: None,
            detail: None,
        },
        CaseRecord {
            id: "q02".to_owned(),
            verdict: Verdict::Wrong,
            latency_ms: 300,
            row_recall: Some(0.5),
            column_recall: None,
            jaccard: Some(0.4),
            rep_verdicts: None,
            detail: Some("row count mismatch".to_owned()),
        },
        CaseRecord {
            id: "q03".to_owned(),
            verdict: Verdict::NoBind,
            latency_ms: 100,
            row_recall: None,
            column_recall: None,
            jaccard: None,
            rep_verdicts: None,
            detail: None,
        },
    ];
    let record = make_run(cases, 1, Some("0.37.0"));
    let md = markdown::render(&record);

    // Advisory line present.
    assert!(md.contains("advisory"), "advisory present: {md}");
    assert!(md.contains("recall"), "recall in advisory: {md}");
    assert!(md.contains("jaccard"), "jaccard in advisory: {md}");

    // Verdict groups.
    assert!(md.contains("## PASS (1)"), "PASS group: {md}");
    assert!(md.contains("## WRONG (1)"), "WRONG group: {md}");
    assert!(md.contains("## NO_BIND (1)"), "NO_BIND group: {md}");

    // Per-case metric badges.
    assert!(md.contains("- q01: recall 100.0%"), "q01 badge: {md}");
    assert!(md.contains("- q02: recall 50.0%"), "q02 badge: {md}");
}

// ──────────────────────────────────────────────────────────────────────────────
// AC3: scalar-only run — verdict groups, no metric lines
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn ac3_scalar_run_no_metrics() {
    let cases = vec![
        make_case("q01", Verdict::Correct),
        make_case("q02", Verdict::Correct),
        make_case("q03", Verdict::Wrong),
    ];
    let record = make_run(cases, 1, None);
    let md = markdown::render(&record);

    assert!(md.contains("## PASS (2)"), "PASS group: {md}");
    assert!(md.contains("## WRONG (1)"), "WRONG group: {md}");
    // No metrics.
    assert!(!md.contains("advisory"), "no advisory: {md}");
    assert!(!md.contains("recall"), "no recall: {md}");
    // No version when absent.
    assert!(!md.contains("null"), "no null version: {md}");
    assert!(!md.contains("None"), "no None version: {md}");
}

// ──────────────────────────────────────────────────────────────────────────────
// AC4: repeat run — per-case consistency noted
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn ac4_repeat_run_consistency_noted() {
    let cases = vec![CaseRecord {
        id: "q01".to_owned(),
        verdict: Verdict::Correct,
        latency_ms: 200,
        row_recall: None,
        column_recall: None,
        jaccard: None,
        rep_verdicts: Some(vec![
            Verdict::Correct,
            Verdict::Correct,
            Verdict::Wrong,
            Verdict::Correct,
        ]),
        detail: None,
    }];
    let record = make_run(cases, 4, None);
    let md = markdown::render(&record);

    // Title notes k=4.
    assert!(md.contains("k=4"), "k=4 in title: {md}");
    // Per-case rep consistency.
    assert!(md.contains("3/4"), "3/4 reps: {md}");
}

// ──────────────────────────────────────────────────────────────────────────────
// AC5: deterministic — two renders are byte-identical
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn ac5_deterministic_output() {
    let cases = vec![
        make_case("q03", Verdict::Correct),
        make_case("q01", Verdict::Wrong),
        make_case("q02", Verdict::NoBind),
    ];
    let record = make_run(cases, 1, Some("0.37.0"));
    assert_eq!(
        markdown::render(&record),
        markdown::render(&record),
        "two renders must be byte-identical"
    );
}

// ──────────────────────────────────────────────────────────────────────────────
// AC7 edge: empty run → valid "no active cases" summary
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn ac7_empty_run_valid_summary() {
    let record = make_run(vec![], 1, None);
    let md = markdown::render(&record);

    assert!(md.starts_with("# tpcds_qwf20"), "H1: {md}");
    assert!(md.contains("0/0 passed"), "zero result: {md}");
    // No WRONG or NO_BIND sections for empty run.
    assert!(!md.contains("## WRONG"), "no WRONG: {md}");
    assert!(!md.contains("## NO_BIND"), "no NO_BIND: {md}");
}

// ──────────────────────────────────────────────────────────────────────────────
// AC7 edge: no version → title omits version (not "null")
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn ac7_no_version_no_null_in_title() {
    let cases = vec![make_case("q01", Verdict::Correct)];
    let record = make_run(cases, 1, None);
    let md = markdown::render(&record);
    assert!(!md.contains("null"), "no null: {md}");
    assert!(!md.contains("None"), "no None: {md}");
}

// ──────────────────────────────────────────────────────────────────────────────
// report --markdown subcommand integration test
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn report_markdown_subcommand() {
    use std::io::Write as _;
    use std::process::Command;

    // Build a minimal RunRecord as JSON.
    let record_json = serde_json::json!({
        "run_id": "20260616-test0001",
        "started_at": "2026-06-16T10:00:00Z",
        "finished_at": "2026-06-16T10:01:00Z",
        "agent": "mqo-agent",
        "server": {"name": "fixture"},
        "corpus_id": "tpcds_test",
        "config": {
            "repeat": 1,
            "min_pass_reps": 1,
            "pass_threshold": 1.0,
            "model": "claude-sonnet-4-5",
            "oracle_mode": "fixture",
            "max_result_rows": 1000
        },
        "cases": [
            {"id": "q01", "verdict": "correct", "latency_ms": 200}
        ],
        "summary": {
            "accuracy": 1.0,
            "no_bind_rate": 0.0,
            "mean_confidence": 0.9,
            "mean_latency_ms": 200.0,
            "total": 1,
            "correct": 1,
            "wrong": 0,
            "no_bind": 0
        }
    });

    let mut tmp = tempfile::NamedTempFile::new().expect("tmp file");
    write!(tmp, "{}", record_json).expect("write json");
    let tmp_path = tmp.path().to_str().expect("path").to_owned();

    // Find the built binary.
    let bin = env!("CARGO_BIN_EXE_mqo-eval");

    let output = Command::new(bin)
        .args(["report", "--markdown", "--run", &tmp_path])
        .output()
        .expect("run mqo-eval");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "exit status: {}\nstderr: {}",
        output.status,
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(stdout.contains("## Result:"), "result line in output: {stdout}");
    assert!(stdout.contains("tpcds_test"), "corpus id in output: {stdout}");
}
