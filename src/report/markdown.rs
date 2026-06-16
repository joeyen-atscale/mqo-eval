//! Markdown `SUMMARY.md` renderer for a [`RunRecord`].
//!
//! Renders a `RunRecord` to a markdown string that mirrors the mcp-tuner
//! `SUMMARY.md` convention: H1 title, `## Result:` line, and per-verdict-class
//! case lists. The output contains only facts present in the record; no
//! narrative prose is synthesized.

use crate::types::{CaseRecord, RunRecord, Verdict};

/// Render a [`RunRecord`] to a CommonMark markdown string.
///
/// The output is deterministic: cases within each verdict group are sorted by
/// case `id`. Two calls with the same record produce byte-identical output.
///
/// # Format
///
/// ```text
/// # <corpus_id> — <agent> / <server_name>[v<mqo_mcp_version>], <oracle_mode>[, k=<repeat>], <started_at>
///
/// ## Result: <correct>/<total> passed (<pct>%)
/// [advisory: mean row recall <X>% · jaccard <Y>%]
///
/// ## PASS (<n>)
/// - <id>[: recall X% · jaccard Y%][  (k/n reps)]
/// ...
///
/// ## WRONG (<n>)
/// - <id>[: recall X% · jaccard Y%][  detail]
/// ...
///
/// ## NO_BIND (<n>)
/// - <id>
/// ...
///
/// [## SKIPPED (<n>)   ← if any]
/// ```
pub fn render(record: &RunRecord) -> String {
    let mut out = String::new();

    // ── H1 title ────────────────────────────────────────────────────────────
    let server_label = match &record.server.mqo_mcp_version {
        Some(v) => format!("{}v{}", record.server.name, v),
        None => record.server.name.clone(),
    };

    let repeat_label = if record.config.repeat > 1 {
        format!(", k={}", record.config.repeat)
    } else {
        String::new()
    };

    out.push_str(&format!(
        "# {} \u{2014} {} / {}, {}{}, {}\n",
        record.corpus_id,
        record.agent,
        server_label,
        record.config.oracle_mode,
        repeat_label,
        record.started_at,
    ));
    out.push('\n');

    // ── Result line ─────────────────────────────────────────────────────────
    let total = record.summary.total;
    let correct = record.summary.correct;
    let pct = if total == 0 {
        0.0_f64
    } else {
        #[allow(clippy::cast_precision_loss)]
        let pct = (correct as f64 / total as f64) * 100.0;
        pct
    };
    out.push_str(&format!(
        "## Result: {}/{} passed ({:.1}%)\n",
        correct, total, pct
    ));

    // ── Advisory means (only when metrics are present) ──────────────────────
    if let Some(advisory) = build_advisory(record) {
        out.push_str(&advisory);
        out.push('\n');
    }

    out.push('\n');

    // ── Group cases by verdict ───────────────────────────────────────────────
    let mut pass_cases: Vec<&CaseRecord> = Vec::new();
    let mut wrong_cases: Vec<&CaseRecord> = Vec::new();
    let mut no_bind_cases: Vec<&CaseRecord> = Vec::new();

    for case in &record.cases {
        match case.verdict {
            Verdict::Correct => pass_cases.push(case),
            Verdict::Wrong => wrong_cases.push(case),
            Verdict::NoBind => no_bind_cases.push(case),
        }
    }

    // Sort each group by id for deterministic output.
    pass_cases.sort_by(|a, b| a.id.cmp(&b.id));
    wrong_cases.sort_by(|a, b| a.id.cmp(&b.id));
    no_bind_cases.sort_by(|a, b| a.id.cmp(&b.id));

    // ── PASS section ────────────────────────────────────────────────────────
    if !pass_cases.is_empty() || total == 0 {
        out.push_str(&format!("## PASS ({})\n", pass_cases.len()));
        for case in &pass_cases {
            out.push_str(&render_case_line(case, false));
        }
        out.push('\n');
    }

    // ── WRONG section ───────────────────────────────────────────────────────
    if !wrong_cases.is_empty() {
        out.push_str(&format!("## WRONG ({})\n", wrong_cases.len()));
        for case in &wrong_cases {
            out.push_str(&render_case_line(case, true));
        }
        out.push('\n');
    }

    // ── NO_BIND section ─────────────────────────────────────────────────────
    if !no_bind_cases.is_empty() {
        out.push_str(&format!("## NO_BIND ({})\n", no_bind_cases.len()));
        for case in &no_bind_cases {
            out.push_str(&render_case_line(case, false));
        }
        out.push('\n');
    }

    out
}

/// Build the advisory line when run-level means are present.
///
/// Scans all cases for metric fields; if any carry `row_recall` or `jaccard`,
/// compute and emit the mean advisory. Returns `None` for scalar runs.
fn build_advisory(record: &RunRecord) -> Option<String> {
    let metric_cases: Vec<&CaseRecord> = record
        .cases
        .iter()
        .filter(|c| c.row_recall.is_some() || c.jaccard.is_some())
        .collect();

    if metric_cases.is_empty() {
        return None;
    }

    let recall_vals: Vec<f64> = metric_cases
        .iter()
        .filter_map(|c| c.row_recall)
        .collect();
    let jaccard_vals: Vec<f64> = metric_cases
        .iter()
        .filter_map(|c| c.jaccard)
        .collect();

    let mut parts: Vec<String> = Vec::new();

    if !recall_vals.is_empty() {
        let mean_recall = recall_vals.iter().sum::<f64>() / recall_vals.len() as f64;
        parts.push(format!("mean row recall {:.1}%", mean_recall * 100.0));
    }
    if !jaccard_vals.is_empty() {
        let mean_jaccard = jaccard_vals.iter().sum::<f64>() / jaccard_vals.len() as f64;
        parts.push(format!("jaccard {:.1}%", mean_jaccard * 100.0));
    }

    if parts.is_empty() {
        None
    } else {
        Some(format!("_advisory: {}_", parts.join(" \u{00B7} ")))
    }
}

/// Render a single case line.
///
/// Format: `- <id>[: recall X% · jaccard Y%][  (k/n reps)][  detail]`
fn render_case_line(case: &CaseRecord, include_detail: bool) -> String {
    let mut line = format!("- {}", case.id);

    // Metrics badge.
    let has_recall = case.row_recall.is_some();
    let has_jaccard = case.jaccard.is_some();
    if has_recall || has_jaccard {
        let mut badge_parts: Vec<String> = Vec::new();
        if let Some(r) = case.row_recall {
            badge_parts.push(format!("recall {:.1}%", r * 100.0));
        }
        if let Some(j) = case.jaccard {
            badge_parts.push(format!("jaccard {:.1}%", j * 100.0));
        }
        line.push_str(&format!(": {}", badge_parts.join(" \u{00B7} ")));
    }

    // Repeat consistency annotation.
    if let Some(reps) = &case.rep_verdicts {
        if reps.len() > 1 {
            let passed = reps.iter().filter(|v| **v == Verdict::Correct).count();
            line.push_str(&format!("  ({}/{}  reps)", passed, reps.len()));
        }
    }

    // Detail (only for WRONG cases, truncated to 120 chars).
    if include_detail {
        if let Some(detail) = &case.detail {
            let truncated = truncate_detail(detail, 120);
            line.push_str(&format!("  {}", truncated));
        }
    }

    line.push('\n');
    line
}

/// Truncate a detail string to `max_chars`, appending `…` when truncated.
fn truncate_detail(s: &str, max_chars: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= max_chars {
        s.to_owned()
    } else {
        // Replace trailing newlines in the detail with a space-safe truncation.
        let truncated: String = chars[..max_chars].iter().collect();
        format!("{}\u{2026}", truncated)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{RunConfigRecord, RunRecord, ServerInfo, SummaryStats, Verdict};

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

    fn make_run(cases: Vec<CaseRecord>, repeat: u32) -> RunRecord {
        let summary = make_summary(&cases);
        RunRecord {
            run_id: "20260616-abcd1234".to_owned(),
            started_at: "2026-06-16T10:00:00Z".to_owned(),
            finished_at: "2026-06-16T10:05:00Z".to_owned(),
            agent: "mqo-agent".to_owned(),
            server: ServerInfo {
                name: "pgwire".to_owned(),
                mqo_mcp_version: Some("0.37.0".to_owned()),
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

    #[test]
    fn render_empty_run() {
        let record = make_run(vec![], 1);
        let md = render(&record);
        assert!(md.contains("0/0 passed"), "should have 0/0: {md}");
        assert!(md.contains("## PASS (0)"), "should have PASS(0): {md}");
        assert!(!md.contains("## WRONG"), "no WRONG for empty: {md}");
        assert!(!md.contains("## NO_BIND"), "no NO_BIND for empty: {md}");
        // Valid markdown: starts with H1
        assert!(md.starts_with("# tpcds_qwf20"), "should start with H1: {md}");
    }

    #[test]
    fn render_scalar_run() {
        let cases = vec![
            make_case("q01", Verdict::Correct),
            make_case("q02", Verdict::Correct),
            make_case("q03", Verdict::Wrong),
        ];
        let record = make_run(cases, 1);
        let md = render(&record);

        assert!(md.contains("## Result: 2/3 passed"), "result line: {md}");
        assert!(md.contains("## PASS (2)"), "PASS group: {md}");
        assert!(md.contains("## WRONG (1)"), "WRONG group: {md}");
        // No metrics on scalar run.
        assert!(!md.contains("advisory"), "no advisory on scalar: {md}");
        assert!(!md.contains("recall"), "no recall on scalar: {md}");
        assert!(!md.contains("jaccard"), "no jaccard on scalar: {md}");
    }

    #[test]
    fn render_metric_run() {
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
        ];
        let record = make_run(cases, 1);
        let md = render(&record);

        assert!(md.contains("advisory"), "advisory present: {md}");
        assert!(md.contains("recall"), "recall present: {md}");
        assert!(md.contains("jaccard"), "jaccard present: {md}");
        // PASS section has q01 with metrics.
        assert!(md.contains("- q01: recall 100.0%"), "q01 metrics: {md}");
        // WRONG section has q02.
        assert!(md.contains("- q02: recall 50.0%"), "q02 metrics: {md}");
    }

    #[test]
    fn deterministic_output() {
        let cases = vec![
            make_case("q03", Verdict::Correct),
            make_case("q01", Verdict::Wrong),
            make_case("q02", Verdict::NoBind),
        ];
        let record = make_run(cases, 1);
        let md1 = render(&record);
        let md2 = render(&record);
        assert_eq!(md1, md2, "two renders must be byte-identical");
    }

    #[test]
    fn sorted_within_group() {
        // Cases inserted in reverse order; output must be sorted by id.
        let cases = vec![
            make_case("q03", Verdict::Correct),
            make_case("q01", Verdict::Correct),
            make_case("q02", Verdict::Correct),
        ];
        let record = make_run(cases, 1);
        let md = render(&record);
        let pass_section = md
            .split("## PASS")
            .nth(1)
            .unwrap_or("");
        let q1_pos = pass_section.find("q01").unwrap_or(usize::MAX);
        let q2_pos = pass_section.find("q02").unwrap_or(usize::MAX);
        let q3_pos = pass_section.find("q03").unwrap_or(usize::MAX);
        assert!(q1_pos < q2_pos, "q01 before q02");
        assert!(q2_pos < q3_pos, "q02 before q03");
    }

    #[test]
    fn no_fabricated_narrative() {
        // The rendered markdown should not contain words that are not in the record.
        let cases = vec![make_case("q01", Verdict::Correct)];
        let record = make_run(cases, 1);
        let md = render(&record);
        // Should NOT contain any of these synthesized/narrative words.
        for word in &["dominant", "cause", "analysis", "inferred", "likely"] {
            assert!(!md.to_lowercase().contains(word), "no narrative word '{word}': {md}");
        }
    }

    #[test]
    fn repeat_k_annotation() {
        let cases = vec![CaseRecord {
            id: "q01".to_owned(),
            verdict: Verdict::Correct,
            latency_ms: 200,
            row_recall: None,
            column_recall: None,
            jaccard: None,
            rep_verdicts: Some(vec![Verdict::Correct, Verdict::Correct, Verdict::Wrong, Verdict::Correct]),
            detail: None,
        }];
        let record = make_run(cases, 4);
        let md = render(&record);
        // Should note k=4 in title.
        assert!(md.contains("k=4"), "k annotation in title: {md}");
        // Should note 3/4 reps in case line.
        assert!(md.contains("3/4"), "rep consistency: {md}");
    }
}
