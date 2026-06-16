//! HTML report renderer for a [`RunRecord`].
//!
//! Produces a single self-contained HTML file (inline CSS, no external assets)
//! that mirrors the structure of `framework/report.py` from `mcp-eval`.

use crate::types::{CaseRecord, RunRecord, Verdict};

// ──────────────────────────────────────────────────────────────────────────────
// Public API
// ──────────────────────────────────────────────────────────────────────────────

/// Render a [`RunRecord`] to a self-contained HTML string.
///
/// The output is deterministic for a fixed input: no clock reads, no random
/// ordering.  All text from the record is HTML-escaped before insertion.
#[must_use]
pub fn render(record: &RunRecord) -> String {
    let mut html = String::with_capacity(8192);

    html.push_str("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\">\n");
    html.push_str("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n");
    let title = esc(&format!("mqo-eval report — {}", record.corpus_id));
    html.push_str(&format!("<title>{title}</title>\n"));
    html.push_str(CSS);
    html.push_str("</head>\n<body>\n");

    html.push_str(&render_banner(record));
    html.push_str(&render_advisory_means(record));
    html.push_str(&render_cases(record));
    html.push_str(&render_footer(record));

    html.push_str("</body>\n</html>\n");
    html
}

// ──────────────────────────────────────────────────────────────────────────────
// Sub-sections
// ──────────────────────────────────────────────────────────────────────────────

fn render_banner(record: &RunRecord) -> String {
    let total = record.summary.total;
    let passed = record.summary.correct;
    let pct = if total == 0 {
        0.0_f64
    } else {
        // truncate to avoid floating-point surprises in tests
        #[allow(clippy::as_conversions, clippy::cast_precision_loss)]
        let p = (passed as f64 / total as f64) * 100.0;
        p
    };

    let (banner_class, rate_label) = if pct >= 80.0 {
        ("banner green", "green")
    } else if pct >= 50.0 {
        ("banner amber", "amber")
    } else {
        ("banner red", "red")
    };

    let cfg = &record.config;
    let repeat_info = if cfg.repeat > 1 {
        format!(
            " · repeat {} · min-pass {} · threshold {:.0}%",
            cfg.repeat,
            cfg.min_pass_reps,
            cfg.pass_threshold * 100.0,
        )
    } else {
        format!(" · threshold {:.0}%", cfg.pass_threshold * 100.0)
    };

    let catalog_info = cfg
        .catalog
        .as_deref()
        .map(|c| format!(" · catalog {}", esc(c)))
        .unwrap_or_default();

    let version_info = record
        .server
        .mqo_mcp_version
        .as_deref()
        .map(|v| format!(" v{}", esc(v)))
        .unwrap_or_default();

    format!(
        r#"<div class="{banner_class}">
  <div class="banner-rate">{passed} / {total} passed ({pct:.1}% — {rate_label})</div>
  <div class="banner-meta">
    agent: <b>{agent}</b> · server: <b>{server}{version_info}</b> · corpus: <b>{corpus}</b>{catalog_info}<br>
    oracle: {oracle}{repeat_info}
  </div>
</div>
"#,
        agent = esc(&record.agent),
        server = esc(&record.server.name),
        corpus = esc(&record.corpus_id),
        oracle = esc(&cfg.oracle_mode),
    )
}

/// Render the advisory result-set means line (omitted when absent).
fn render_advisory_means(record: &RunRecord) -> String {
    // Compute means only over cases that carry metrics.
    let metric_cases: Vec<&CaseRecord> = record
        .cases
        .iter()
        .filter(|c| c.row_recall.is_some() || c.jaccard.is_some())
        .collect();

    if metric_cases.is_empty() {
        return String::new();
    }

    let mean_recall = mean_opt(metric_cases.iter().map(|c| c.row_recall));
    let mean_jaccard = mean_opt(metric_cases.iter().map(|c| c.jaccard));

    let recall_str = pct_str(mean_recall);
    let jaccard_str = pct_str(mean_jaccard);

    format!(
        "<div class=\"advisory-means\">advisory result-set means — recall {recall_str} · jaccard {jaccard_str}</div>\n"
    )
}

fn render_cases(record: &RunRecord) -> String {
    if record.cases.is_empty() {
        return "<div class=\"no-cases\">No cases in this run.</div>\n".to_owned();
    }

    let mut out = String::with_capacity(1024);
    out.push_str("<ul class=\"case-list\">\n");
    for case in &record.cases {
        out.push_str(&render_case(case));
    }
    out.push_str("</ul>\n");
    out
}

fn render_case(case: &CaseRecord) -> String {
    let (icon, verdict_class) = verdict_icon_class(&case.verdict);
    let id_esc = esc(&case.id);
    let verdict_label = esc(&case.verdict.to_string());

    let mut li = format!(
        "  <li class=\"case {verdict_class}\">\n    <span class=\"icon\">{icon}</span> <span class=\"case-id\">{id_esc}</span> <span class=\"verdict\">{verdict_label}</span>"
    );

    // Metric badge
    if case.row_recall.is_some() || case.jaccard.is_some() || case.column_recall.is_some() {
        li.push(' ');
        li.push_str(&render_metric_badge(case));
    }

    li.push('\n');

    // Consistency line (repeat > 1)
    if let Some(reps) = &case.rep_verdicts {
        let n = reps.len();
        let k = reps.iter().filter(|v| **v == Verdict::Correct).count();
        let glyphs: String = reps
            .iter()
            .map(|v| match v {
                Verdict::Correct => '✓',
                Verdict::Wrong => '✗',
                Verdict::NoBind => '○',
            })
            .collect();
        li.push_str(&format!(
            "    <div class=\"consistency\">{k}/{n} passed [{glyphs}]</div>\n"
        ));
    }

    // Detail line
    if let Some(detail) = &case.detail {
        li.push_str(&format!(
            "    <div class=\"detail\">{}</div>\n",
            esc(detail)
        ));
    }

    li.push_str("  </li>\n");
    li
}

fn render_metric_badge(case: &CaseRecord) -> String {
    let recall = pct_str(case.row_recall);
    let jaccard = pct_str(case.jaccard);
    let cols = pct_str(case.column_recall);
    format!("<span class=\"badge\">[recall {recall} · jaccard {jaccard} · cols {cols}]</span>")
}

fn render_footer(record: &RunRecord) -> String {
    format!(
        "<div class=\"footer\">corpus: {} · run: {}</div>\n",
        esc(&record.corpus_id),
        esc(&record.run_id),
    )
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

/// HTML-escape a string.  Escapes `&`, `<`, `>`, `"`, `'`.
fn esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            c => out.push(c),
        }
    }
    out
}

/// Returns `(icon, css_class)` for a verdict.
fn verdict_icon_class(v: &Verdict) -> (&'static str, &'static str) {
    match v {
        Verdict::Correct => ("✓", "correct"),
        Verdict::Wrong => ("✗", "wrong"),
        Verdict::NoBind => ("○", "no-bind"),
    }
}

/// Format an optional fraction as a percentage string, or `—` if absent.
fn pct_str(v: Option<f64>) -> String {
    v.map_or_else(|| "\u{2014}".to_owned(), |f| format!("{:.0}%", f * 100.0))
}

/// Arithmetic mean of an iterator of `Option<f64>`, treating `None` as absent
/// (not as 0).  Returns `None` when the iterator is empty or all-`None`.
fn mean_opt<'a>(iter: impl Iterator<Item = Option<f64>>) -> Option<f64> {
    let mut sum = 0.0_f64;
    let mut count = 0usize;
    for v in iter {
        if let Some(f) = v {
            sum += f;
            count += 1;
        }
    }
    if count == 0 {
        None
    } else {
        #[allow(clippy::as_conversions, clippy::cast_precision_loss)]
        Some(sum / count as f64)
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Inline CSS
// ──────────────────────────────────────────────────────────────────────────────

const CSS: &str = r#"<style>
body {
  font-family: 'Menlo', 'Consolas', 'Courier New', monospace;
  font-size: 13px;
  background: #fafafa;
  color: #222;
  margin: 0;
  padding: 16px;
}
.banner {
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 12px;
}
.banner.green  { background: #d4edda; border: 1px solid #28a745; }
.banner.amber  { background: #fff3cd; border: 1px solid #ffc107; }
.banner.red    { background: #f8d7da; border: 1px solid #dc3545; }
.banner-rate   { font-size: 1.3em; font-weight: bold; margin-bottom: 4px; }
.banner-meta   { color: #444; }
.advisory-means {
  background: #e8f4fd;
  border: 1px solid #90cdf4;
  border-radius: 4px;
  padding: 6px 12px;
  margin-bottom: 12px;
  color: #2c5282;
}
.case-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.case {
  padding: 6px 10px;
  margin: 3px 0;
  border-radius: 4px;
  border-left: 4px solid transparent;
  line-height: 1.5;
}
.case.correct  { background: #f0fff4; border-color: #28a745; }
.case.wrong    { background: #fff5f5; border-color: #dc3545; }
.case.no-bind  { background: #f7fafc; border-color: #a0aec0; color: #718096; }
.icon { font-size: 1.1em; }
.case.correct .icon { color: #28a745; }
.case.wrong   .icon { color: #dc3545; }
.case.no-bind .icon { color: #a0aec0; }
.case-id   { font-weight: bold; margin: 0 4px; }
.verdict   { color: #555; margin-right: 6px; }
.badge {
  font-size: 0.85em;
  background: #edf2f7;
  border: 1px solid #cbd5e0;
  border-radius: 3px;
  padding: 1px 5px;
  color: #2d3748;
}
.consistency {
  font-size: 0.85em;
  color: #666;
  margin-left: 20px;
}
.detail {
  font-size: 0.85em;
  color: #718096;
  margin-left: 20px;
  white-space: pre-wrap;
  word-break: break-word;
}
.no-cases {
  color: #888;
  padding: 12px;
}
.footer {
  margin-top: 16px;
  font-size: 0.8em;
  color: #aaa;
  border-top: 1px solid #eee;
  padding-top: 8px;
}
</style>
"#;

// ──────────────────────────────────────────────────────────────────────────────
// Unit tests (inline)
// ──────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::missing_panics_doc,
    clippy::float_arithmetic,
    clippy::as_conversions,
    clippy::uninlined_format_args,
)]
mod tests {
    use super::*;
    use crate::types::{RunConfigRecord, RunRecord, ServerInfo, SummaryStats};

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

    fn make_summary(total: usize, correct: usize) -> SummaryStats {
        #[allow(clippy::as_conversions, clippy::cast_precision_loss)]
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

    // ── render_empty_run ─────────────────────────────────────────────────────

    #[test]
    fn render_empty_run() {
        let record = empty_run();
        let html = render(&record);
        assert!(html.contains("<!DOCTYPE html>"), "must be HTML");
        assert!(html.contains("</html>"), "must close html");
        assert!(html.contains("0 / 0 passed"), "banner present");
        assert!(!html.contains("advisory result-set means"), "no means on empty run");
    }

    // ── render_scalar_run ────────────────────────────────────────────────────

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
        let html = render(&record);
        assert!(html.contains("1 / 3 passed"), "banner shows 1/3");
        assert!(html.contains("✓"), "correct icon");
        assert!(html.contains("✗"), "wrong icon");
        assert!(html.contains("○"), "nobind icon");
        assert!(!html.contains("advisory result-set means"), "no means without metrics");
        // CSS contains ".badge" class definition; check no badge *content* is rendered.
        assert!(!html.contains("recall "), "no recall badge content without metrics");
    }

    // ── render_metric_run ────────────────────────────────────────────────────

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
        let html = render(&record);
        assert!(html.contains("advisory result-set means"), "advisory means present");
        assert!(html.contains("badge"), "metric badge present");
        assert!(html.contains("recall 95%"), "recall badge value");
        assert!(html.contains("jaccard 90%"), "jaccard badge value");
    }

    // ── html_escape_case_id ──────────────────────────────────────────────────

    #[test]
    fn html_escape_case_id() {
        let mut c = case("<script>alert(1)</script>", Verdict::Correct);
        c.detail = Some("<img src=x onerror=alert(1)>".to_owned());

        let record = RunRecord {
            cases: vec![c],
            summary: make_summary(1, 1),
            ..empty_run()
        };
        let html = render(&record);
        assert!(!html.contains("<script>"), "script tag must be escaped");
        assert!(html.contains("&lt;script&gt;"), "script must be HTML-escaped");
        assert!(!html.contains("<img"), "img tag must be escaped");
    }

    // ── banner_color_thresholds ──────────────────────────────────────────────

    #[test]
    fn banner_color_green() {
        let record = RunRecord {
            cases: vec![
                case("q1", Verdict::Correct),
                case("q2", Verdict::Correct),
                case("q3", Verdict::Correct),
                case("q4", Verdict::Correct),
                case("q5", Verdict::Correct),
            ],
            summary: make_summary(5, 5),
            ..empty_run()
        };
        let html = render(&record);
        assert!(html.contains("banner green"), "5/5 → green banner");
    }

    #[test]
    fn banner_color_amber() {
        let record = RunRecord {
            cases: vec![
                case("q1", Verdict::Correct),
                case("q2", Verdict::Correct),
                case("q3", Verdict::Correct),
                case("q4", Verdict::Wrong),
                case("q5", Verdict::Wrong),
            ],
            summary: make_summary(5, 3),
            ..empty_run()
        };
        let html = render(&record);
        assert!(html.contains("banner amber"), "3/5=60% → amber banner");
    }

    #[test]
    fn banner_color_red() {
        let record = RunRecord {
            cases: vec![
                case("q1", Verdict::Correct),
                case("q2", Verdict::Wrong),
                case("q3", Verdict::Wrong),
                case("q4", Verdict::Wrong),
                case("q5", Verdict::Wrong),
            ],
            summary: make_summary(5, 1),
            ..empty_run()
        };
        let html = render(&record);
        assert!(html.contains("banner red"), "1/5=20% → red banner");
    }

    // ── esc helper ───────────────────────────────────────────────────────────

    #[test]
    fn esc_all_special_chars() {
        assert_eq!(esc("&<>\"'"), "&amp;&lt;&gt;&quot;&#39;");
        assert_eq!(esc("hello"), "hello");
    }

    // ── pct_str helper ───────────────────────────────────────────────────────

    #[test]
    fn pct_str_some() {
        assert_eq!(pct_str(Some(0.956)), "96%");
        assert_eq!(pct_str(Some(0.0)), "0%");
        assert_eq!(pct_str(Some(1.0)), "100%");
    }

    #[test]
    fn pct_str_none() {
        assert_eq!(pct_str(None), "\u{2014}");
    }

    // ── consistency line (rep_verdicts) ──────────────────────────────────────

    #[test]
    fn consistency_line_rendered() {
        let mut c = case("q1", Verdict::Correct);
        c.rep_verdicts = Some(vec![
            Verdict::Correct,
            Verdict::Wrong,
            Verdict::Correct,
            Verdict::Correct,
        ]);
        let record = RunRecord {
            cases: vec![c],
            summary: make_summary(1, 1),
            config: make_config(4),
            ..empty_run()
        };
        let html = render(&record);
        assert!(html.contains("3/4 passed"), "consistency k/n present");
        assert!(html.contains("✓✗✓✓"), "per-rep glyphs present");
    }
}
