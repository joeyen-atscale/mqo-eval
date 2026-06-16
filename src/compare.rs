//! The `compare` command — diff two run results files for verdict flips.

#![allow(clippy::print_stdout)]

use anyhow::{Context, Result};

use crate::types::{QuestionResult, VerdictFlip};

/// Compare two results files and return questions that flipped verdict.
///
/// # Errors
///
/// Returns an error if either file cannot be read or parsed.
pub fn compare_files(path_a: &str, path_b: &str) -> Result<Vec<VerdictFlip>> {
    let results_a = load_results(path_a)?;
    let results_b = load_results(path_b)?;
    Ok(find_flips(&results_a, &results_b))
}

fn load_results(path: &str) -> Result<Vec<QuestionResult>> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read results file: {path}"))?;
    crate::summary::load_results_from_text(&text, path)
}

/// Find verdict flips between two result sets (matched by question ID).
#[must_use]
pub fn find_flips(a: &[QuestionResult], b: &[QuestionResult]) -> Vec<VerdictFlip> {
    use std::collections::HashMap;

    // Index B by question ID.
    let b_index: HashMap<&str, &QuestionResult> =
        b.iter().map(|r| (r.id.as_str(), r)).collect();

    a.iter()
        .filter_map(|ra| {
            let rb = b_index.get(ra.id.as_str())?;
            if ra.verdict == rb.verdict {
                return None;
            }
            Some(VerdictFlip {
                id: ra.id.clone(),
                question: ra.question.clone(),
                verdict_a: ra.verdict.clone(),
                verdict_b: rb.verdict.clone(),
            })
        })
        .collect()
}

/// Print verdict flips in text format.
pub fn print_text(flips: &[VerdictFlip]) {
    if flips.is_empty() {
        println!("No verdict flips between the two runs.");
        return;
    }
    println!("{} verdict flip(s):", flips.len());
    for f in flips {
        println!("  [{}] {} → {}: {}", f.id, f.verdict_a, f.verdict_b, f.question);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{QuestionResult, Verdict};

    fn make_result(id: &str, verdict: Verdict) -> QuestionResult {
        QuestionResult {
            question: format!("Question {id}"),
            id: id.to_owned(),
            verdict,
            confidence: 0.5,
            pillars_fired: vec![],
            latency_ms: 10,
        }
    }

    #[test]
    fn find_flips_detects_changes() {
        let a = vec![
            make_result("q1", Verdict::Correct),
            make_result("q2", Verdict::Wrong),
            make_result("q3", Verdict::NoBind),
        ];
        let b = vec![
            make_result("q1", Verdict::Wrong),   // flip
            make_result("q2", Verdict::Wrong),   // same
            make_result("q3", Verdict::Correct), // flip
        ];
        let flips = find_flips(&a, &b);
        assert_eq!(flips.len(), 2);
        let ids: Vec<&str> = flips.iter().map(|f| f.id.as_str()).collect();
        assert!(ids.contains(&"q1"));
        assert!(ids.contains(&"q3"));
    }

    #[test]
    fn find_flips_none_when_same() {
        let a = vec![make_result("q1", Verdict::Correct)];
        let b = vec![make_result("q1", Verdict::Correct)];
        assert!(find_flips(&a, &b).is_empty());
    }

    #[test]
    fn find_flips_missing_in_b() {
        // q2 only in A; should be silently skipped (no flip).
        let a = vec![
            make_result("q1", Verdict::Correct),
            make_result("q2", Verdict::Wrong),
        ];
        let b = vec![make_result("q1", Verdict::Wrong)];
        let flips = find_flips(&a, &b);
        assert_eq!(flips.len(), 1);
        if let Some(flip) = flips.first() {
            assert_eq!(flip.id, "q1");
        }
    }
}
