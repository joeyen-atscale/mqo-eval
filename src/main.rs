//! mqo-eval — LLM-free eval harness driven by mqo-agent.
//!
//! Drive question sets through `mqo-agent` (or a stub in CI) and grade
//! results without any LLM API key.

#![allow(clippy::print_stdout, clippy::print_stderr)]

use clap::{Parser, Subcommand};
use std::process::ExitCode;

use mqo_eval::{
    compare,
    run::{run, OutputFormat, RunConfig},
    summary,
    types::OracleMode,
};

#[derive(Debug, Parser)]
#[command(name = "mqo-eval", version, about = "LLM-free MCP eval harness")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
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

        /// Path to write the legacy flat results JSON file.
        #[arg(long, default_value = "results.json", value_name = "FILE")]
        out: String,

        /// Output format: `text` or `json`.
        #[arg(long, default_value = "text", value_name = "FORMAT")]
        format: String,

        /// Root directory for the structured run archive.
        #[arg(long, default_value = "results", value_name = "DIR")]
        results_dir: String,

        /// `AtScale` catalog name (forwarded to agent; stored in run record).
        #[arg(long, value_name = "CATALOG")]
        catalog: Option<String>,

        /// Maximum result rows per query (stored in run record).
        #[arg(long, default_value = "1000", value_name = "N")]
        max_result_rows: u64,
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
            out,
            format,
            results_dir,
            catalog,
            max_result_rows,
        } => {
            let oracle_mode = oracle
                .parse::<OracleMode>()
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            let fmt = format
                .parse::<OutputFormat>()
                .map_err(|e| anyhow::anyhow!("{e}"))?;

            let config = RunConfig {
                questions_path: questions,
                model,
                agent_cmd: agent,
                oracle: oracle_mode,
                pg_host,
                pg_pass_env,
                out_path: out,
                format: fmt,
                results_dir,
                catalog,
                max_result_rows,
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
    }

    Ok(())
}
