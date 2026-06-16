//! Shared data types for mqo-eval.

use serde::{Deserialize, Serialize};

// ──────────────────────────────────────────────────────────────────────────────
// RunRecord and friends (PRD-mqoeval-run-record-archive)
// ──────────────────────────────────────────────────────────────────────────────

/// Identity and version of the MQO server that served the run.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerInfo {
    /// Short label, e.g. `"pgwire"` or `"fixture"`.
    pub name: String,
    /// Version string reported by the MQO server, if available.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mqo_mcp_version: Option<String>,
}

/// Snapshot of all user-configurable knobs for a single run.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunConfigRecord {
    /// Number of repetitions per question (1 = single-shot).
    pub repeat: u32,
    /// Minimum passing repetitions required for `correct` verdict (repeat ≥ 2).
    pub min_pass_reps: u32,
    /// Score threshold for `correct` (fraction, 0.0–1.0).
    pub pass_threshold: f64,
    /// `AtScale` catalog name, if provided.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub catalog: Option<String>,
    /// LLM model ID used by the agent.
    pub model: String,
    /// Oracle mode label (`"fixture"` or `"pgwire"`).
    pub oracle_mode: String,
    /// Maximum result rows returned per query.
    pub max_result_rows: u64,
}

/// Per-case record inside a `RunRecord`.
///
/// Metric fields (`row_recall`, `column_recall`, `jaccard`) are `Option`
/// because they are absent on scalar runs and populated only by
/// handle-grading (PRD-mqoeval-handle-grading). They serialize as `null`
/// when absent, not as `0`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaseRecord {
    /// Question identifier.
    pub id: String,
    /// Final grading verdict.
    pub verdict: Verdict,
    /// Wall-clock latency of this case in milliseconds.
    pub latency_ms: u64,
    /// Fraction of expected result rows that were returned (handle-grading).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub row_recall: Option<f64>,
    /// Fraction of expected result columns that were returned (handle-grading).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub column_recall: Option<f64>,
    /// Jaccard similarity between returned and expected row sets (handle-grading).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub jaccard: Option<f64>,
    /// Per-repetition verdict list (populated when `repeat > 1`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rep_verdicts: Option<Vec<Verdict>>,
    /// Optional free-form detail / error message.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// The canonical archive record written for every completed run.
///
/// Written to `results/<agent>/<server>/<corpus_id>/<run_id>.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunRecord {
    /// Unique, time-sortable run identifier (`<compact_ts>-<corpus8>`).
    pub run_id: String,
    /// ISO-8601 timestamp of run start.
    pub started_at: String,
    /// ISO-8601 timestamp of run completion.
    pub finished_at: String,
    /// Basename of the agent command binary.
    pub agent: String,
    /// MQO server identity and version.
    pub server: ServerInfo,
    /// Corpus identifier (filename stem of the questions file).
    pub corpus_id: String,
    /// Frozen snapshot of the run configuration.
    pub config: RunConfigRecord,
    /// Per-case results.
    pub cases: Vec<CaseRecord>,
    /// Aggregate summary statistics recomputed from `cases`.
    pub summary: SummaryStats,
}

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
