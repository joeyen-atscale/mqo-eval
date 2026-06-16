//! The `run` command — drives questions through a binder and grades results.

#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::path::Path;
use std::time::Instant;

use anyhow::{bail, Context, Result};
use serde_json::Value;

use crate::{
    oracle,
    summary,
    types::{
        CaseRecord, OracleMode, Question, QuestionsFile, QuestionResult, RunConfigRecord,
        RunRecord, ServerInfo,
    },
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
    /// Output path for results JSON (legacy flat array; optional).
    pub out_path: String,
    /// Output format (text or json).
    pub format: OutputFormat,
    /// Root directory for the structured run archive (`results/` by default).
    pub results_dir: String,
    /// `AtScale` catalog name, if provided.
    pub catalog: Option<String>,
    /// Maximum result rows per query (forwarded to agent; stored in record).
    pub max_result_rows: u64,
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
/// Also writes a structured [`RunRecord`] to the archive directory tree:
/// `<results_dir>/<agent>/<server>/<corpus_id>/<run_id>.json`.
///
/// # Errors
///
/// Returns an error if the questions file cannot be read, the oracle is
/// misconfigured, or writing the output fails.
pub fn run(config: &RunConfig) -> Result<Vec<QuestionResult>> {
    // Capture run start time once, before any work.
    let run_wall_start = std::time::SystemTime::now();
    let started_at = format_system_time(run_wall_start);

    // Validate pgwire config eagerly — fail fast before any questions run.
    if config.oracle == OracleMode::Pgwire {
        oracle::pgwire_password(&config.pg_pass_env).context("pgwire oracle pre-check")?;
    }

    let yaml_text = std::fs::read_to_string(&config.questions_path)
        .with_context(|| format!("failed to read questions file: {}", config.questions_path))?;

    let qfile: QuestionsFile =
        serde_yaml::from_str(&yaml_text).context("failed to parse questions YAML")?;

    let mut results = Vec::with_capacity(qfile.questions.len());
    let mut case_records = Vec::with_capacity(qfile.questions.len());

    for q in &qfile.questions {
        let result = evaluate_question(q, config)?;
        // Build a CaseRecord from this QuestionResult.
        let case = CaseRecord {
            id: result.id.clone(),
            verdict: result.verdict.clone(),
            latency_ms: result.latency_ms,
            row_recall: None,
            column_recall: None,
            jaccard: None,
            rep_verdicts: None,
            detail: None,
        };
        case_records.push(case);
        results.push(result);
    }

    let finished_at = format_system_time(std::time::SystemTime::now());

    // Write legacy flat results.json (backward compatibility, FR5).
    let json = serde_json::to_string_pretty(&results).context("failed to serialize results")?;
    std::fs::write(&config.out_path, &json)
        .with_context(|| format!("failed to write results to {}", config.out_path))?;

    // Assemble and write the structured RunRecord archive (FR1–FR4).
    let record = assemble_run_record(
        config,
        case_records,
        &results,
        started_at,
        finished_at,
    );
    write_run_record(config, &record)?;

    // Print summary to stdout.
    match config.format {
        OutputFormat::Text => print_text_summary(&results),
        OutputFormat::Json => println!("{json}"),
    }

    Ok(results)
}

/// Format a [`std::time::SystemTime`] as a compact ISO-8601-like string.
///
/// Uses `YYYYMMDDTHHMMSSZ` for the `run_id` component and a fuller
/// `YYYY-MM-DDTHH:MM:SSZ` for the human-readable timestamp fields.
fn format_system_time(t: std::time::SystemTime) -> String {
    // Seconds since Unix epoch.
    let secs = t
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    secs_to_iso8601(secs)
}

/// Convert Unix seconds to a naive ISO-8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`).
fn secs_to_iso8601(secs: u64) -> String {
    // Hand-roll without chrono to keep dependencies minimal.
    let sec = secs % 60;
    let min = (secs / 60) % 60;
    let hour = (secs / 3600) % 24;
    let days = secs / 86400;
    let (year, month, day) = days_to_ymd(days);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{min:02}:{sec:02}Z")
}

/// Convert a compact `ISO`-like string to the `run_id` prefix format (remove `-`, `:`).
fn iso_to_compact(iso: &str) -> String {
    iso.chars().filter(char::is_ascii_alphanumeric).collect()
}

/// Compute year/month/day from days since Unix epoch (1970-01-01).
///
/// Uses the proleptic Gregorian calendar.
///
/// Algorithm: <http://howardhinnant.github.io/date_algorithms.html>
/// (`civil_from_days`, signed → adjusted for u64 epoch origin).
const fn days_to_ymd(days: u64) -> (u64, u64, u64) {
    let z = days + 719_468;
    let era = z / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era = (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146_096) / 365;
    let year_raw = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let mp = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { year_raw + 1 } else { year_raw };
    (year, month, day)
}

/// Derive the `<server>` path segment from the oracle mode.
const fn server_label(oracle: &OracleMode) -> &'static str {
    match oracle {
        OracleMode::Pgwire => "pgwire",
        OracleMode::Fixture => "fixture",
    }
}

/// Assemble a [`RunRecord`] from a completed run.
fn assemble_run_record(
    config: &RunConfig,
    cases: Vec<CaseRecord>,
    results: &[QuestionResult],
    started_at: String,
    finished_at: String,
) -> RunRecord {
    let corpus_id = corpus_id_from_path(&config.questions_path);
    let agent_name = agent_basename(&config.agent_cmd);
    let server_name = server_label(&config.oracle).to_owned();

    // run_id = <compact_ts>-<first8_of_corpus_id>
    let compact_ts = iso_to_compact(&started_at);
    let corpus8: String = corpus_id.chars().take(8).collect();
    let run_id = format!("{compact_ts}-{corpus8}");

    let summary = summary::summarize(results);

    let run_config_record = RunConfigRecord {
        repeat: 1,
        min_pass_reps: 1,
        pass_threshold: 1.0,
        catalog: config.catalog.clone(),
        model: config.model.clone(),
        oracle_mode: server_name.clone(),
        max_result_rows: config.max_result_rows,
    };

    RunRecord {
        run_id,
        started_at,
        finished_at,
        agent: agent_name,
        server: ServerInfo {
            name: server_name,
            mqo_mcp_version: None,
        },
        corpus_id,
        config: run_config_record,
        cases,
        summary,
    }
}

/// Extract corpus id from the questions file path (filename stem).
fn corpus_id_from_path(path: &str) -> String {
    Path::new(path)
        .file_stem()
        .map_or_else(|| "unknown".to_owned(), |s| s.to_string_lossy().into_owned())
}

/// Extract the basename of the agent command (first whitespace-separated token).
fn agent_basename(agent_cmd: &str) -> String {
    let first_token = agent_cmd.split_whitespace().next().unwrap_or(agent_cmd);
    Path::new(first_token)
        .file_name()
        .map_or_else(|| first_token.to_owned(), |s| s.to_string_lossy().into_owned())
}

/// Write a `RunRecord` to the structured archive path (atomic: temp → rename).
///
/// Path: `<results_dir>/<agent>/<server>/<corpus_id>/<run_id>.json`.
/// Refuses to overwrite an existing file (FR4).
///
/// # Errors
///
/// Returns an error if directory creation, file write, or rename fails, or if
/// the target archive file already exists.
fn write_run_record(config: &RunConfig, record: &RunRecord) -> Result<()> {
    let dir = Path::new(&config.results_dir)
        .join(&record.agent)
        .join(&record.server.name)
        .join(&record.corpus_id);

    std::fs::create_dir_all(&dir)
        .with_context(|| format!("failed to create archive directory: {}", dir.display()))?;

    let target = dir.join(format!("{}.json", record.run_id));
    if target.exists() {
        // Never overwrite an existing run (FR4 / AC2).
        return Err(anyhow::anyhow!(
            "run archive file already exists (refusing to overwrite): {}",
            target.display()
        ));
    }

    let tmp_path = dir.join(format!("{}.json.tmp", record.run_id));
    let json =
        serde_json::to_string_pretty(record).context("failed to serialize RunRecord")?;

    std::fs::write(&tmp_path, &json)
        .with_context(|| format!("failed to write tmp archive: {}", tmp_path.display()))?;

    std::fs::rename(&tmp_path, &target)
        .with_context(|| format!("failed to rename archive file to {}", target.display()))?;

    Ok(())
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
        assert!(conf.abs() < f64::EPSILON);
        assert!(pillars.is_empty());
    }

    #[test]
    fn parse_agent_output_empty() {
        let (ans, _, _) = parse_agent_output("  ");
        assert!(ans.is_none());
    }
}
