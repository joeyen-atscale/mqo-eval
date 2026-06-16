//! Shared data types for mqo-eval.

use serde::{Deserialize, Serialize};

/// A single question entry from questions.yaml.
#[derive(Debug, Clone, Deserialize)]
pub struct Question {
    /// Unique question identifier.
    pub id: String,
    /// Natural-language question text.
    #[serde(alias = "question")]
    pub text: String,
    /// Expected answer string (or numeric value as string).
    pub expected_answer: String,
    /// Model path in `catalog/model` form.
    pub model: String,
}

/// Top-level questions YAML structure.
#[derive(Debug, Deserialize)]
pub struct QuestionsFile {
    /// The list of questions.
    pub questions: Vec<Question>,
}

/// Verdict for a single question evaluation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// Agent answer matched expected answer within tolerance.
    Correct,
    /// Agent answer did not match expected answer.
    Wrong,
    /// Agent returned no bound MQO (binding failed).
    NoBind,
}

impl std::fmt::Display for Verdict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Correct => write!(f, "correct"),
            Self::Wrong => write!(f, "wrong"),
            Self::NoBind => write!(f, "no_bind"),
        }
    }
}

/// Per-question result entry (output row in results.json).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuestionResult {
    /// The question text.
    pub question: String,
    /// The question ID.
    pub id: String,
    /// Grading verdict.
    pub verdict: Verdict,
    /// Confidence score from the agent (0.0–1.0).
    pub confidence: f64,
    /// Pillar names that fired during evaluation.
    pub pillars_fired: Vec<String>,
    /// Wall-clock latency in milliseconds.
    pub latency_ms: u64,
}

/// Summary statistics compatible with mcp-eval output format.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SummaryStats {
    /// Fraction of questions with `correct` verdict (0.0–1.0).
    pub accuracy: f64,
    /// Fraction of questions with `no_bind` verdict (0.0–1.0).
    pub no_bind_rate: f64,
    /// Mean confidence across all questions.
    pub mean_confidence: f64,
    /// Mean latency in milliseconds across all questions.
    pub mean_latency_ms: f64,
    /// Total number of questions evaluated.
    pub total: usize,
    /// Number of correct answers.
    pub correct: usize,
    /// Number of wrong answers.
    pub wrong: usize,
    /// Number of no-bind results.
    pub no_bind: usize,
}

/// A verdict flip between two runs of the same question.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerdictFlip {
    /// Question ID.
    pub id: String,
    /// Question text.
    pub question: String,
    /// Verdict in run A.
    pub verdict_a: Verdict,
    /// Verdict in run B.
    pub verdict_b: Verdict,
}

/// Which oracle mode to use for grading.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OracleMode {
    /// Use pre-computed fixture answers (offline, no network).
    Fixture,
    /// Execute against a live PGWire/AtScale endpoint.
    Pgwire,
}

impl std::str::FromStr for OracleMode {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "fixture" => Ok(Self::Fixture),
            "pgwire" => Ok(Self::Pgwire),
            other => Err(format!("unknown oracle mode: {other}; expected 'fixture' or 'pgwire'")),
        }
    }
}
