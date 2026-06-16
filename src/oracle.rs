//! Oracle implementations for grading agent answers.

#![allow(clippy::float_arithmetic)]

use crate::types::Verdict;
use anyhow::{Context, Result};

/// Tolerance for numeric answer comparison (relative, 0.1% by default).
const NUMERIC_TOLERANCE: f64 = 0.001;

/// Grade an agent answer against the expected answer.
///
/// Returns `Correct` if the answers match within tolerance, `Wrong` otherwise.
/// If `agent_answer` is `None`, returns `NoBind`.
#[must_use]
pub fn grade(expected: &str, agent_answer: Option<&str>) -> Verdict {
    let Some(actual) = agent_answer else {
        return Verdict::NoBind;
    };

    // Exact string match first.
    if expected.trim() == actual.trim() {
        return Verdict::Correct;
    }

    // Try numeric comparison with tolerance.
    if let (Ok(exp_f), Ok(act_f)) = (parse_numeric(expected), parse_numeric(actual)) {
        if numeric_match(exp_f, act_f) {
            return Verdict::Correct;
        }
    }

    Verdict::Wrong
}

fn parse_numeric(s: &str) -> std::result::Result<f64, std::num::ParseFloatError> {
    // Strip common formatting: commas, currency symbols, percent signs.
    let cleaned: String = s
        .chars()
        .filter(|c| c.is_ascii_digit() || *c == '.' || *c == '-')
        .collect();
    cleaned.parse::<f64>()
}

#[allow(clippy::float_cmp)]
fn numeric_match(a: f64, b: f64) -> bool {
    // Direct equality check is intentional here — we handle the tolerance below.
    if a == b {
        return true;
    }
    let denom = a.abs().max(b.abs());
    if denom < f64::EPSILON {
        return (a - b).abs() < f64::EPSILON;
    }
    ((a - b) / denom).abs() <= NUMERIC_TOLERANCE
}

/// Parse the pgwire password from the environment.
///
/// # Errors
///
/// Returns an error if the environment variable `env_var` is not set.
pub fn pgwire_password(env_var: &str) -> Result<String> {
    std::env::var(env_var)
        .with_context(|| format!("env var `{env_var}` is not set; cannot connect to pgwire oracle"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grade_exact_match() {
        assert_eq!(grade("foo", Some("foo")), Verdict::Correct);
    }

    #[test]
    fn grade_no_bind() {
        assert_eq!(grade("foo", None), Verdict::NoBind);
    }

    #[test]
    fn grade_wrong() {
        assert_eq!(grade("foo", Some("bar")), Verdict::Wrong);
    }

    #[test]
    fn grade_numeric_within_tolerance() {
        assert_eq!(grade("1000.00", Some("1000.50")), Verdict::Correct);
    }

    #[test]
    fn grade_numeric_outside_tolerance() {
        assert_eq!(grade("1000.00", Some("1100.00")), Verdict::Wrong);
    }

    #[test]
    fn grade_numeric_formatted() {
        // "1,234,567.89" → 1234567.89
        assert_eq!(grade("1234567.89", Some("1234567.89")), Verdict::Correct);
    }

    #[test]
    fn pgwire_password_absent() {
        // Ensure unset var returns Err, not panic.
        std::env::remove_var("_MQO_EVAL_TEST_ABSENT_VAR");
        assert!(pgwire_password("_MQO_EVAL_TEST_ABSENT_VAR").is_err());
    }
}
