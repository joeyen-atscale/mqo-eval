//! The `run` command — drives questions through a binder and grades results.

#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::time::Instant;

use anyhow::{bail, Context, Result};
use serde_json::Value;

use crate::{
    oracle,
    types::{OracleMode, Question, QuestionsFile, QuestionResult},
};

/// Configuration for a single `run` invocation.
#[derive(Debug, Clone)]
pub struct RunConfig {
    /// Path to questions YAML file.
    pub questions_path: String,
    /// Model path in `catalog/model` form.
    pub model: String,
    /// Agent command template (subprocess override).
    pub agent_cmd: String,
    /// Oracle mode.
    pub oracle: OracleMode,
    /// `PGWire` host (used only with Pgwire oracle).
    pub pg_host: Option<String>,
    /// Environment variable name that holds the PG password.
    pub pg_pass_env: String,
    /// Output path for results JSON.
    pub out_path: String,
    /// Output format (text or json).
    pub format: OutputFormat,
}

/// Output format for the run command.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutputFormat {
    /// Human-readable text.
    Text,
    /// Machine-readable JSON.
    Json,
}

impl std::str::FromStr for OutputFormat {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "text" => Ok(Self::Text),
            "json" => Ok(Self::Json),
            other => Err(format!("unknown format: {other}; expected 'text' or 'json'")),
        }
    }
}

/// Execute a run and return the per-question results.
///
/// # Errors
///
/// Returns an error if the questions file cannot be read, the oracle is
/// misconfigured, or writing the output fails.
pub fn run(config: &RunConfig) -> Result<Vec<QuestionResult>> {
    // Validate pgwire config eagerly — fail fast before any questions run.
    if config.oracle == OracleMode::Pgwire {
        oracle::pgwire_password(&config.pg_pass_env).context("pgwire oracle pre-check")?;
    }

    let yaml_text = std::fs::read_to_string(&config.questions_path)
        .with_context(|| format!("failed to read questions file: {}", config.questions_path))?;

    let qfile: QuestionsFile =
        serde_yaml::from_str(&yaml_text).context("failed to parse questions YAML")?;

    let mut results = Vec::with_capacity(qfile.questions.len());

    for q in &qfile.questions {
        let result = evaluate_question(q, config)?;
        results.push(result);
    }

    // Write results.json.
    let json = serde_json::to_string_pretty(&results).context("failed to serialize results")?;
    std::fs::write(&config.out_path, &json)
        .with_context(|| format!("failed to write results to {}", config.out_path))?;

    // Print summary to stdout.
    match config.format {
        OutputFormat::Text => print_text_summary(&results),
        OutputFormat::Json => println!("{json}"),
    }

    Ok(results)
}

fn evaluate_question(q: &Question, config: &RunConfig) -> Result<QuestionResult> {
    let start = Instant::now();

    // Build the agent command: replace `{}` or append the question.
    let cmd = build_agent_cmd(&config.agent_cmd, &q.text, &config.model);

    // Run the binder subprocess.
    let agent_output = invoke_agent(&cmd)?;

    let latency_ms = u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX);

    // Parse agent output as JSON.
    let (bound_answer, confidence, pillars_fired) = parse_agent_output(&agent_output);

    // Grade.
    let verdict = oracle::grade(&q.expected_answer, bound_answer.as_deref());

    Ok(QuestionResult {
        question: q.text.clone(),
        id: q.id.clone(),
        verdict,
        confidence,
        pillars_fired,
        latency_ms,
    })
}

/// Build the shell command for the agent.
///
/// If the template contains `{}`, substitute question text; otherwise append it.
fn build_agent_cmd(template: &str, question: &str, model: &str) -> String {
    // Escape single quotes in the question for shell safety.
    let safe_q = question.replace('\'', r"'\''");
    let safe_m = model.replace('\'', r"'\''");

    if template.contains("{}") {
        #[allow(clippy::literal_string_with_formatting_args)]
        let result = template
            .replace("{}", &format!("'{safe_q}'"))
            .replace("{model}", &format!("'{safe_m}'"));
        result
    } else {
        format!("{template} ask '{safe_q}' --model '{safe_m}'")
    }
}

/// Invoke the agent command and capture stdout.
fn invoke_agent(cmd: &str) -> Result<String> {
    let output = std::process::Command::new("sh")
        .arg("-c")
        .arg(cmd)
        .output()
        .with_context(|| format!("failed to spawn agent command: {cmd}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!(
            "agent command exited with {}: {stderr}",
            output.status.code().unwrap_or(-1)
        );
    }

    String::from_utf8(output.stdout).context("agent output is not valid UTF-8")
}

/// Parse the agent JSON output into (answer, confidence, `pillars_fired`).
///
/// Expected JSON shape:
/// ```json
/// {"bound_mqo": "...", "result_rows": [...], "confidence": 0.9, "pillars_fired": ["bind"]}
/// ```
fn parse_agent_output(output: &str) -> (Option<String>, f64, Vec<String>) {
    let Ok(val) = serde_json::from_str::<Value>(output.trim()) else {
        // Non-JSON output → treat as plain answer string.
        let trimmed = output.trim();
        if trimmed.is_empty() {
            return (None, 0.0, vec![]);
        }
        return (Some(trimmed.to_owned()), 0.0, vec![]);
    };

    // If it's a plain JSON scalar (not an object), use it directly as the answer.
    if val.is_string() || val.is_number() || val.is_boolean() {
        let ans = val.to_string().trim_matches('"').to_owned();
        let ans = if ans.is_empty() { None } else { Some(ans) };
        return (ans, 0.0, vec![]);
    }

    // Extract answer from structured object: prefer result_rows, then bound_mqo.
    let answer = extract_answer(&val);
    let confidence = val
        .get("confidence")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let pillars_fired = val
        .get("pillars_fired")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();

    (answer, confidence, pillars_fired)
}

fn extract_answer(val: &Value) -> Option<String> {
    // Try result_rows[0][0] first.
    if let Some(rows) = val.get("result_rows").and_then(Value::as_array) {
        if let Some(first_row) = rows.first() {
            if let Some(first_cell) = first_row.as_array().and_then(|r| r.first()) {
                return Some(first_cell.to_string().trim_matches('"').to_owned());
            }
            // Row might be an object; take first value.
            if let Some(obj) = first_row.as_object() {
                if let Some(v) = obj.values().next() {
                    return Some(v.to_string().trim_matches('"').to_owned());
                }
            }
        }
    }

    // Fall back to bound_mqo field.
    val.get("bound_mqo")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

fn print_text_summary(results: &[QuestionResult]) {
    for r in results {
        println!("[{}] {} — {}", r.verdict, r.id, r.question);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_agent_cmd_append_mode() {
        let cmd = build_agent_cmd("mqo-agent", "What is revenue?", "tb/sales");
        assert!(cmd.contains("ask"));
        assert!(cmd.contains("What is revenue?"));
    }

    #[test]
    fn parse_agent_output_json() {
        let json = r#"{"bound_mqo":"x","result_rows":[],"confidence":0.9,"pillars_fired":["bind"]}"#;
        let (ans, conf, pillars) = parse_agent_output(json);
        // No result_rows → falls back to bound_mqo.
        assert_eq!(ans, Some("x".to_owned()));
        assert!((conf - 0.9).abs() < 1e-9);
        assert_eq!(pillars, vec!["bind"]);
    }

    #[test]
    fn parse_agent_output_plain_string() {
        let (ans, conf, pillars) = parse_agent_output("42.0\n");
        assert_eq!(ans, Some("42.0".to_owned()));
        assert_eq!(conf, 0.0);
        assert!(pillars.is_empty());
    }

    #[test]
    fn parse_agent_output_empty() {
        let (ans, _, _) = parse_agent_output("  ");
        assert!(ans.is_none());
    }
}
