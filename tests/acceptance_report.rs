//! Acceptance tests for the HTML run report (PRD-mqoeval-html-report).
//!
//! Covers:
//!  - render_empty_run: valid HTML for 0 cases
//!  - render_scalar_run: 3 cases, no metrics, correct icons
//!  - render_metric_run: advisory means + badges when metrics present
//!  - html_escape_case_id: injection-safe
//!  - banner_color_thresholds: green/amber/red
//!  - report_subcommand_writes_file: end-to-end CLI test

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::missing_panics_doc,
    clippy::float_arithmetic,
    clippy::as_conversions,
    clippy::uninlined_format_args,
)]

use mqo_eval::{
    report::html,
    types::{CaseRecord, RunConfigRecord, RunRecord, ServerInfo, SummaryStats, Verdict},
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
        model: "tb/sales".to_owned(),
        oracle_mode: "fixture".to_owned(),
        max_result_rows: 1000,
    }
}

fn make_server() -> ServerInfo {
    ServerInfo {
        name: "fixture".to_owned(),
        mqo_mcp_version: None,
    }
}

#[allow(clippy::cast_precision_loss)]
fn make_summary(total: usize, correct: usize) -> SummaryStats {
    let accuracy = if total == 0 { 0.0 } else { correct as f64 / total as f64 };
    SummaryStats {
        accuracy,
        no_bind_rate: 0.0,
        mean_confidence: 0.0,
        mean_latency_ms: 0.0,
        total,
        correct,
        wrong: total - correct,
        no_bind: 0,
    }
}

fn empty_run() -> RunRecord {
    RunRecord {
        run_id: "test-run".to_owned(),
        started_at: "2026-01-01T00:00:00Z".to_owned(),
        finished_at: "2026-01-01T00:00:01Z".to_owned(),
        agent: "mqo-agent".to_owned(),
        server: make_server(),
        corpus_id: "test_corpus".to_owned(),
        config: make_config(1),
        cases: vec![],
        summary: make_summary(0, 0),
    }
}

fn case(id: &str, verdict: Verdict) -> CaseRecord {
    CaseRecord {
        id: id.to_owned(),
        verdict,
        latency_ms: 10,
        row_recall: None,
        column_recall: None,
        jaccard: None,
        rep_verdicts: None,
        detail: None,
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Test: render_empty_run
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn render_empty_run() {
    let record = empty_run();
    let out = html::render(&record);
    assert!(out.contains("<!DOCTYPE html>"), "must start with DOCTYPE");
    assert!(out.contains("</html>"), "must close html tag");
    assert!(out.contains("0 / 0 passed"), "banner shows 0/0");
    assert!(!out.contains("advisory result-set means"), "no means on empty run");
}

// ──────────────────────────────────────────────────────────────────────────────
// Test: render_scalar_run
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn render_scalar_run() {
    let record = RunRecord {
        cases: vec![
            case("q1", Verdict::Correct),
            case("q2", Verdict::Wrong),
            case("q3", Verdict::NoBind),
        ],
        summary: make_summary(3, 1),
        ..empty_run()
    };
    let out = html::render(&record);
    assert!(out.contains("1 / 3 passed"), "banner shows 1/3");
    assert!(out.contains("✓"), "correct icon");
    assert!(out.contains("✗"), "wrong icon");
    assert!(out.contains("○"), "no-bind icon");
    assert!(!out.contains("advisory result-set means"), "no means without metrics");
    // CSS contains ".badge" class definition; check no badge *content* is rendered.
    assert!(!out.contains("recall "), "no recall badge content without metrics");
}

// ──────────────────────────────────────────────────────────────────────────────
// Test: render_metric_run
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn render_metric_run() {
    let mut c1 = case("q1", Verdict::Correct);
    c1.row_recall = Some(0.95);
    c1.jaccard = Some(0.90);
    c1.column_recall = Some(1.0);

    let mut c2 = case("q2", Verdict::Wrong);
    c2.row_recall = Some(0.50);
    c2.jaccard = Some(0.40);
    c2.column_recall = Some(0.80);

    let record = RunRecord {
        cases: vec![c1, c2],
        summary: make_summary(2, 1),
        ..empty_run()
    };
    let out = html::render(&record);
    assert!(out.contains("advisory result-set means"), "advisory means line present");
    assert!(out.contains("badge"), "metric badge class present");
    assert!(out.contains("recall 95%"), "row_recall badge value");
    assert!(out.contains("jaccard 90%"), "jaccard badge value");
    assert!(out.contains("cols 100%"), "column_recall badge value");
}

// ──────────────────────────────────────────────────────────────────────────────
// Test: html_escape_case_id
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn html_escape_case_id() {
    let mut c = case("<script>alert(1)</script>", Verdict::Correct);
    c.detail = Some("<img src=x onerror=alert(1)>".to_owned());

    let record = RunRecord {
        cases: vec![c],
        summary: make_summary(1, 1),
        ..empty_run()
    };
    let out = html::render(&record);
    assert!(!out.contains("<script>"), "script tag must be escaped");
    assert!(out.contains("&lt;script&gt;"), "script must be HTML-escaped");
    assert!(!out.contains("<img "), "img tag must be escaped");
    assert!(out.contains("&lt;img"), "img must be HTML-escaped");
}

// ──────────────────────────────────────────────────────────────────────────────
// Test: banner_color_thresholds
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn banner_color_thresholds_green() {
    let record = RunRecord {
        cases: (1..=5).map(|i| case(&format!("q{i}"), Verdict::Correct)).collect(),
        summary: make_summary(5, 5),
        ..empty_run()
    };
    assert!(html::render(&record).contains("banner green"), "5/5 → green");
}

#[test]
fn banner_color_thresholds_amber() {
    let mut cases: Vec<CaseRecord> = (1..=3).map(|i| case(&format!("q{i}"), Verdict::Correct)).collect();
    cases.extend((4..=5).map(|i| case(&format!("q{i}"), Verdict::Wrong)));
    let record = RunRecord {
        cases,
        summary: make_summary(5, 3),
        ..empty_run()
    };
    assert!(html::render(&record).contains("banner amber"), "3/5=60% → amber");
}

#[test]
fn banner_color_thresholds_red() {
    let mut cases = vec![case("q1", Verdict::Correct)];
    cases.extend((2..=5).map(|i| case(&format!("q{i}"), Verdict::Wrong)));
    let record = RunRecord {
        cases,
        summary: make_summary(5, 1),
        ..empty_run()
    };
    assert!(html::render(&record).contains("banner red"), "1/5=20% → red");
}

// ──────────────────────────────────────────────────────────────────────────────
// Test: report_subcommand_writes_file
// ──────────────────────────────────────────────────────────────────────────────

#[test]
fn report_subcommand_writes_file() {
    use std::process::Command;

    let tmp = tempfile::tempdir().expect("tempdir");
    let record = RunRecord {
        cases: vec![
            case("q1", Verdict::Correct),
            case("q2", Verdict::Wrong),
        ],
        summary: make_summary(2, 1),
        ..empty_run()
    };

    let input_path = tmp.path().join("run_record.json");
    std::fs::write(
        &input_path,
        serde_json::to_string_pretty(&record).expect("serialize"),
    )
    .expect("write input");

    let output_path = tmp.path().join("report.html");

    // Find the built binary. The test binary lives in target/debug/deps/;
    // the mqo-eval binary is one level up in target/debug/.
    let test_exe = std::env::current_exe().expect("current exe");
    let deps_dir = test_exe.parent().expect("deps dir");
    // Walk up until we find the dir that contains the mqo-eval binary.
    let bin = if deps_dir.join("mqo-eval").exists() {
        deps_dir.join("mqo-eval")
    } else {
        deps_dir
            .parent()
            .expect("debug dir")
            .join("mqo-eval")
    };

    let status = Command::new(&bin)
        .args([
            "report",
            "--html",
            "--run",
            input_path.to_str().unwrap(),
            "--out",
            output_path.to_str().unwrap(),
        ])
        .status()
        .expect("run mqo-eval report");

    assert!(status.success(), "mqo-eval report must exit 0");
    let content = std::fs::read_to_string(&output_path).expect("read output");
    assert!(content.contains("<!DOCTYPE html>"), "output must be HTML");
    assert!(content.len() > 100, "output must be non-trivially non-empty");
}
