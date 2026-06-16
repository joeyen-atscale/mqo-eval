//! mqo-eval — LLM-free eval harness driven by mqo-agent.
//!
//! Drive question sets through `mqo-agent` (or a stub in CI) and grade
//! results without any LLM API key.

#![allow(clippy::print_stdout, clippy::print_stderr)]

use clap::{Parser, Subcommand};
use std::process::ExitCode;

use mqo_eval::{
    compare,
    pgwire::{DEFAULT_CATALOG_NAME, DEFAULT_MODEL_NAME, HARD_ROW_CEIL},
    report,
    run::{run, OutputFormat, RunConfig},
    summary,
    types::{OracleMode, RunRecord},
};

#[derive(Debug, Parser)]
#[command(name = "mqo-eval", version, about = "LLM-free MCP eval harness")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
#[allow(clippy::large_enum_variant)]
enum Command {
    /// Drive questions through the binder and grade results.
    Run {
        /// Path to questions YAML file.
        #[arg(long, value_name = "FILE")]
        questions: String,

        /// Model path in `catalog/model` form.
        #[arg(long, value_name = "CATALOG/MODEL")]
        model: String,

        /// Agent command to invoke (defaults to `mqo-agent`).
        #[arg(long, default_value = "mqo-agent", value_name = "CMD")]
        agent: String,

        /// Oracle mode: `fixture` (offline) or `pgwire` (live cluster).
        #[arg(long, default_value = "fixture", value_name = "MODE")]
        oracle: String,

        /// `PGWire` host (required when --oracle pgwire).
        #[arg(long, value_name = "HOST")]
        pg_host: Option<String>,

        /// Environment variable holding the PG password.
        #[arg(long, default_value = "ATSCALE_PG_PASS", value_name = "ENV_VAR")]
        pg_pass_env: String,

        /// Catalog name substituted for `${CatalogName}` in `expected_sql`.
        #[arg(long, default_value_t = DEFAULT_CATALOG_NAME.to_owned(), value_name = "CATALOG")]
        catalog_name: String,

        /// Model name substituted for `${ModelName}` in `expected_sql`.
        #[arg(long, default_value_t = DEFAULT_MODEL_NAME.to_owned(), value_name = "MODEL")]
        model_name: String,

        /// Maximum rows fetched per golden query (hard ceil 200000).
        #[arg(long, default_value_t = 50_000_u64, value_name = "N")]
        max_result_rows: u64,

        /// `PGWire` user (defaults to `atscale`).
        #[arg(long, default_value = "atscale", value_name = "USER")]
        pg_user: String,

        /// Path to write the legacy flat results JSON file.
        #[arg(long, default_value = "results.json", value_name = "FILE")]
        out: String,

        /// Output format: `text` or `json`.
        #[arg(long, default_value = "text", value_name = "FORMAT")]
        format: String,

        /// Root directory for the structured run archive.
        #[arg(long, default_value = "results", value_name = "DIR")]
        results_dir: String,
    },

    /// Print aggregate statistics from a results file.
    Summary {
        /// Path to results JSON produced by `run`.
        #[arg(long, value_name = "FILE")]
        results: String,

        /// Output format: `text` or `json`.
        #[arg(long, default_value = "text", value_name = "FORMAT")]
        format: String,
    },

    /// Report verdict flips between two result runs.
    Compare {
        /// First results file.
        #[arg(long, value_name = "FILE")]
        a: String,

        /// Second results file.
        #[arg(long, value_name = "FILE")]
        b: String,

        /// Output format: `text` or `json`.
        #[arg(long, default_value = "text", value_name = "FORMAT")]
        format: String,
    },

    /// Render a run record as an HTML report.
    Report {
        /// Render as self-contained HTML.
        #[arg(long)]
        html: bool,

        /// Path to the RunRecord JSON file produced by `run`.
        #[arg(long, value_name = "FILE")]
        run: String,

        /// Output path for the HTML file (stdout if omitted).
        #[arg(long, value_name = "FILE")]
        out: Option<String>,
    },
}

fn main() -> ExitCode {
    sigpipe::reset();
    match run_main() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

fn run_main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Run {
            questions,
            model,
            agent,
            oracle,
            pg_host,
            pg_pass_env,
            catalog_name,
            model_name,
            max_result_rows,
            pg_user,
            out,
            format,
            results_dir,
        } => {
            let oracle_mode = oracle
                .parse::<OracleMode>()
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            let fmt = format
                .parse::<OutputFormat>()
                .map_err(|e| anyhow::anyhow!("{e}"))?;

            // Clamp max_result_rows to the hard ceiling.
            let max_result_rows = max_result_rows.min(HARD_ROW_CEIL);

            let config = RunConfig {
                questions_path: questions,
                model,
                agent_cmd: agent,
                oracle: oracle_mode,
                pg_host,
                pg_pass_env,
                catalog_name,
                model_name,
                max_result_rows,
                pg_user,
                out_path: out,
                format: fmt,
                results_dir,
            };

            let results = run(&config)?;
            eprintln!("evaluated {} questions → {}", results.len(), config.out_path);
        }

        Command::Summary { results, format } => {
            let stats = summary::summarize_file(&results)?;
            let fmt = format
                .parse::<OutputFormat>()
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            match fmt {
                OutputFormat::Text => summary::print_text(&stats),
                OutputFormat::Json => {
                    let json = serde_json::to_string_pretty(&stats)?;
                    println!("{json}");
                }
            }
        }

        Command::Compare { a, b, format } => {
            let flips = compare::compare_files(&a, &b)?;
            let fmt = format
                .parse::<OutputFormat>()
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            match fmt {
                OutputFormat::Text => compare::print_text(&flips),
                OutputFormat::Json => {
                    let json = serde_json::to_string_pretty(&flips)?;
                    println!("{json}");
                }
            }
        }

        Command::Report { html, run: run_path, out } => {
            if !html {
                anyhow::bail!("--html is required (it is the only supported format)");
            }
            let content = std::fs::read_to_string(&run_path)
                .map_err(|e| anyhow::anyhow!("cannot read {run_path}: {e}"))?;
            let record = load_run_record(&content)?;
            let rendered = report::html::render(&record);
            match out {
                Some(path) => {
                    std::fs::write(&path, &rendered)
                        .map_err(|e| anyhow::anyhow!("cannot write {path}: {e}"))?;
                    eprintln!("report written to {path}");
                }
                None => {
                    print!("{rendered}");
                }
            }
        }
    }

    Ok(())
}

/// Load a [`RunRecord`] from JSON, accepting both the canonical `RunRecord`
/// shape (has `"cases"` field) and a legacy flat array of [`QuestionResult`].
fn load_run_record(json: &str) -> anyhow::Result<RunRecord> {
    // Try the canonical RunRecord shape first.
    if let Ok(record) = serde_json::from_str::<RunRecord>(json) {
        return Ok(record);
    }
    // Fall back: wrap a legacy flat array into a minimal RunRecord.
    use mqo_eval::types::{CaseRecord, QuestionResult, RunConfigRecord, ServerInfo, SummaryStats, Verdict};
    let flat: Vec<QuestionResult> = serde_json::from_str(json)
        .map_err(|e| anyhow::anyhow!("cannot parse as RunRecord or legacy flat array: {e}"))?;

    let total = flat.len();
    let correct = flat.iter().filter(|q| q.verdict == Verdict::Correct).count();
    let wrong = flat.iter().filter(|q| q.verdict == Verdict::Wrong).count();
    let no_bind = flat.iter().filter(|q| q.verdict == Verdict::NoBind).count();
    #[allow(clippy::as_conversions, clippy::cast_precision_loss)]
    let accuracy = if total == 0 { 0.0 } else { correct as f64 / total as f64 };
    #[allow(clippy::as_conversions, clippy::cast_precision_loss)]
    let no_bind_rate = if total == 0 { 0.0 } else { no_bind as f64 / total as f64 };
    #[allow(clippy::as_conversions, clippy::cast_precision_loss)]
    let mean_confidence = if flat.is_empty() {
        0.0
    } else {
        flat.iter().map(|q| q.confidence).sum::<f64>() / flat.len() as f64
    };
    #[allow(clippy::as_conversions, clippy::cast_precision_loss)]
    let mean_latency_ms = if flat.is_empty() {
        0.0
    } else {
        flat.iter().map(|q| q.latency_ms as f64).sum::<f64>() / flat.len() as f64
    };

    let cases = flat
        .into_iter()
        .map(|q| CaseRecord {
            id: q.id,
            verdict: q.verdict,
            latency_ms: q.latency_ms,
            row_recall: None,
            column_recall: None,
            jaccard: None,
            rep_verdicts: None,
            detail: None,
        })
        .collect();

    Ok(RunRecord {
        run_id: "legacy".to_owned(),
        started_at: String::new(),
        finished_at: String::new(),
        agent: "unknown".to_owned(),
        server: ServerInfo { name: "unknown".to_owned(), mqo_mcp_version: None },
        corpus_id: "legacy".to_owned(),
        config: RunConfigRecord {
            repeat: 1,
            min_pass_reps: 1,
            pass_threshold: 1.0,
            catalog: None,
            model: "unknown".to_owned(),
            oracle_mode: "unknown".to_owned(),
            max_result_rows: 1000,
        },
        cases,
        summary: SummaryStats {
            accuracy,
            no_bind_rate,
            mean_confidence,
            mean_latency_ms,
            total,
            correct,
            wrong,
            no_bind,
        },
    })
}
