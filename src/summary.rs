//! The `summary` command — aggregate statistics over a results file.

#![allow(clippy::print_stdout, clippy::float_arithmetic, clippy::cast_precision_loss)]

use anyhow::{Context, Result};

use crate::types::{QuestionResult, SummaryStats, Verdict};

/// Compute summary statistics from a results file path.
///
/// Accepts both a legacy flat `results.json` (a JSON array of
/// [`QuestionResult`]) and a structured [`RunRecord`] (an object with a
/// `"cases"` key). The shape is detected automatically; the embedded summary
/// inside a `RunRecord` is ignored and recomputed.
///
/// # Errors
///
/// Returns an error if the file cannot be read or parsed.
pub fn summarize_file(results_path: &str) -> Result<SummaryStats> {
    let text = std::fs::read_to_string(results_path)
        .with_context(|| format!("failed to read results file: {results_path}"))?;
    let results = load_results_from_text(&text, results_path)?;
    Ok(summarize(&results))
}

/// Parse a JSON string into a flat `Vec<QuestionResult>`, tolerating both a
/// legacy flat array and a `RunRecord`-shaped object (`{ "cases": [...] }`).
///
/// # Errors
///
/// Returns an error if the input is neither a valid flat array nor a
/// recognisable `RunRecord`.
pub fn load_results_from_text(text: &str, source_label: &str) -> Result<Vec<QuestionResult>> {
    use serde_json::Value;
    let val: Value = serde_json::from_str(text)
        .with_context(|| format!("failed to parse JSON from {source_label}"))?;

    if val.is_array() {
        // Legacy flat array.
        serde_json::from_value(val)
            .with_context(|| format!("failed to deserialise flat results array from {source_label}"))
    } else if val.is_object() && val.get("cases").is_some() {
        // RunRecord — extract and convert cases to QuestionResult.
        use crate::types::CaseRecord;
        let cases: Vec<CaseRecord> = serde_json::from_value(
            val.get("cases").cloned().unwrap_or(Value::Array(vec![])),
        )
        .with_context(|| format!("failed to deserialise RunRecord.cases from {source_label}"))?;

        let results = cases
            .into_iter()
            .map(|c| QuestionResult {
                question: c.id.clone(),
                id: c.id,
                verdict: c.verdict,
                confidence: 0.0,
                pillars_fired: vec![],
                latency_ms: c.latency_ms,
            })
            .collect();
        Ok(results)
    } else {
        anyhow::bail!(
            "unrecognised results format in {source_label}: expected a JSON array or a RunRecord object"
        )
    }
}

/// Compute summary statistics from an in-memory slice of results.
#[must_use]
pub fn summarize(results: &[QuestionResult]) -> SummaryStats {
    let total = results.len();
    if total == 0 {
        return SummaryStats {
            accuracy: 0.0,
            no_bind_rate: 0.0,
            mean_confidence: 0.0,
            mean_latency_ms: 0.0,
            total: 0,
            correct: 0,
            wrong: 0,
            no_bind: 0,
        };
    }

    let correct = results.iter().filter(|r| r.verdict == Verdict::Correct).count();
    let no_bind = results.iter().filter(|r| r.verdict == Verdict::NoBind).count();
    let wrong = total - correct - no_bind;

    #[allow(clippy::as_conversions)]
    let total_f = total as f64;
    let mean_confidence = results.iter().map(|r| r.confidence).sum::<f64>() / total_f;
    #[allow(clippy::as_conversions)]
    let mean_latency_ms =
        results.iter().map(|r| r.latency_ms as f64).sum::<f64>() / total_f;

    #[allow(clippy::as_conversions)]
    SummaryStats {
        accuracy: correct as f64 / total_f,
        no_bind_rate: no_bind as f64 / total_f,
        mean_confidence,
        mean_latency_ms,
        total,
        correct,
        wrong,
        no_bind,
    }
}

/// Print a text-format summary to stdout.
pub fn print_text(stats: &SummaryStats) {
    println!("accuracy:       {:.1}%", stats.accuracy * 100.0);
    println!("no_bind_rate:   {:.1}%", stats.no_bind_rate * 100.0);
    println!("mean_confidence:{:.3}", stats.mean_confidence);
    println!("mean_latency_ms:{:.1}", stats.mean_latency_ms);
    println!(
        "total: {}  correct: {}  wrong: {}  no_bind: {}",
        stats.total, stats.correct, stats.wrong, stats.no_bind
    );
}

#[cfg(test)]
#[allow(clippy::float_cmp)]
mod tests {
    use super::*;
    use crate::types::QuestionResult;

    fn make_result(verdict: Verdict, confidence: f64, latency_ms: u64) -> QuestionResult {
        QuestionResult {
            question: "Q".to_owned(),
            id: "q1".to_owned(),
            verdict,
            confidence,
            pillars_fired: vec![],
            latency_ms,
            oracle_outcome: None,
        }
    }

    #[test]
    fn summarize_empty() {
        let stats = summarize(&[]);
        assert_eq!(stats.total, 0);
        assert!(stats.accuracy.abs() < f64::EPSILON);
    }

    #[test]
    fn summarize_known_counts() {
        let results = vec![
            make_result(Verdict::Correct, 0.9, 100),
            make_result(Verdict::Correct, 0.8, 200),
            make_result(Verdict::Wrong, 0.3, 50),
            make_result(Verdict::NoBind, 0.0, 10),
            make_result(Verdict::Wrong, 0.5, 150),
        ];
        let stats = summarize(&results);
        assert_eq!(stats.total, 5);
        assert_eq!(stats.correct, 2);
        assert_eq!(stats.wrong, 2);
        assert_eq!(stats.no_bind, 1);
        // accuracy = 2/5 = 0.4
        assert!((stats.accuracy - 0.4).abs() < 1e-9);
        // no_bind_rate = 1/5 = 0.2
        assert!((stats.no_bind_rate - 0.2).abs() < 1e-9);
        // mean_confidence = (0.9+0.8+0.3+0.0+0.5)/5 = 0.5
        assert!((stats.mean_confidence - 0.5).abs() < 1e-9);
        // mean_latency = (100+200+50+10+150)/5 = 102
        assert!((stats.mean_latency_ms - 102.0).abs() < 1e-9);
    }
}
