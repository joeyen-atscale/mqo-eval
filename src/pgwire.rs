//! `PGWire` oracle: connects to `AtScale`'s `Postgres` wire endpoint and
//! executes golden SQL, returning a typed [`OracleOutcome`].

#![allow(clippy::float_arithmetic)]

use anyhow::{Context, Result};
use tokio_postgres::types::Type;

use crate::types::{Cell, OracleOutcome, ResultTable};

/// Hard ceiling on the number of rows that may be fetched from a single query.
pub const HARD_ROW_CEIL: u64 = 200_000;

/// Default `PGWire` port used by `AtScale`.
const PG_PORT: u16 = 15432;

/// Default catalog name substituted for `${CatalogName}`.
pub const DEFAULT_CATALOG_NAME: &str = "atscale_catalogs";

/// Default model name substituted for `${ModelName}`.
pub const DEFAULT_MODEL_NAME: &str = "tpcds_benchmark_model";

/// Parameters used by the `PGWire` oracle.
#[derive(Debug, Clone)]
pub struct PgwireConfig {
    /// `PGWire` host (e.g. `mcp-aws.atscaleinternal.com`).
    pub pg_host: String,
    /// `PGWire` user (defaults to `atscale`).
    pub pg_user: String,
    /// Name of the env var that holds the PG password.
    pub pg_pass_env: String,
    /// Database name (defaults to empty string, which `AtScale` accepts).
    pub pg_dbname: String,
    /// Catalog name for `${CatalogName}` substitution.
    pub catalog_name: String,
    /// Model name for `${ModelName}` substitution.
    pub model_name: String,
    /// Per-run row cap (must be ≤ [`HARD_ROW_CEIL`]).
    pub max_result_rows: u64,
}

impl PgwireConfig {
    /// Build a Postgres connection string from this config.
    ///
    /// The password is read from the env var at call time; it is never stored
    /// in a field and never returned in an error message.
    ///
    /// # Errors
    ///
    /// Returns an error if the password env var is not set.
    fn connstr(&self) -> Result<String> {
        let pass = std::env::var(&self.pg_pass_env).with_context(|| {
            format!(
                "env var `{}` is not set; cannot connect to PGWire oracle",
                self.pg_pass_env
            )
        })?;
        Ok(format!(
            "host={} port={} user={} password={} dbname={}",
            self.pg_host, PG_PORT, self.pg_user, pass, self.pg_dbname
        ))
    }
}

/// Substitute `${CatalogName}` and `${ModelName}` placeholders in `sql`.
#[must_use]
pub fn substitute_placeholders(sql: &str, catalog: &str, model: &str) -> String {
    sql.replace("${CatalogName}", catalog)
        .replace("${ModelName}", model)
}

/// Perform an eager connection pre-check (connect + `SELECT 1`).
///
/// Called before any case is graded; aborts the run on failure.
///
/// # Errors
///
/// Returns a human-readable error (no credentials) if the connection fails.
pub async fn pgwire_precheck(cfg: &PgwireConfig) -> Result<()> {
    let connstr = cfg.connstr()?;
    let (client, conn) = tokio_postgres::connect(&connstr, tokio_postgres::NoTls)
        .await
        .with_context(|| {
            format!(
                "PGWire pre-check: cannot connect to {}:{} (check host, port, credentials)",
                cfg.pg_host, PG_PORT
            )
        })?;

    // Drive the connection on a background task.
    let _conn_handle = tokio::spawn(async move {
        // We ignore the connection error here; any real error will surface
        // when `client` attempts to use the connection.
        let _ = conn.await;
    });

    client
        .simple_query("SELECT 1")
        .await
        .with_context(|| format!("PGWire pre-check: SELECT 1 failed on {}:{}", cfg.pg_host, PG_PORT))?;

    Ok(())
}

/// Execute a single golden SQL statement and return a typed [`OracleOutcome`].
///
/// Template substitution is applied before execution.  The `case_id` is used
/// only in error payloads (never in connection strings).
///
/// # Errors
///
/// Returns an error only if establishing the connection itself fails (i.e. the
/// caller should handle this as a fatal run error, not a per-case error).
/// Per-case SQL errors are returned as [`OracleOutcome::OracleError`].
pub async fn execute_golden(
    cfg: &PgwireConfig,
    case_id: &str,
    expected_sql: &str,
) -> Result<OracleOutcome> {
    let sql = substitute_placeholders(expected_sql, &cfg.catalog_name, &cfg.model_name);

    // Strip a trailing semicolon so tokio-postgres doesn't reject it as
    // a multi-statement query.
    let sql = sql.trim().trim_end_matches(';').trim().to_owned();

    let connstr = cfg.connstr()?;
    let (client, conn) = match tokio_postgres::connect(&connstr, tokio_postgres::NoTls).await {
        Ok(pair) => pair,
        Err(e) => {
            return Ok(OracleOutcome::OracleError {
                case_id: case_id.to_owned(),
                message: format!("connection failed: {e}"),
            });
        }
    };

    let _conn_handle = tokio::spawn(async move {
        let _ = conn.await;
    });

    let rows = match client.query(sql.as_str(), &[]).await {
        Ok(r) => r,
        Err(e) => {
            return Ok(OracleOutcome::OracleError {
                case_id: case_id.to_owned(),
                message: format!("query error: {e}"),
            });
        }
    };

    if rows.is_empty() {
        // Build column list from the statement description if possible.
        // `tokio-postgres` exposes column metadata on any row; with 0 rows
        // we cannot reach it via rows[0].  We return Empty per AC5.
        return Ok(OracleOutcome::Empty);
    }

    // Column names from the first row.
    let columns: Vec<String> = rows
        .first()
        .map(|r| r.columns().iter().map(|c| c.name().to_owned()).collect())
        .unwrap_or_default();

    let cap = cfg.max_result_rows.min(HARD_ROW_CEIL);
    let row_count = u64::try_from(rows.len()).unwrap_or(u64::MAX);

    if row_count > cap {
        return Ok(OracleOutcome::Oversize {
            observed_at_least: row_count,
            cap,
        });
    }

    let mut typed_rows: Vec<Vec<Cell>> = Vec::with_capacity(rows.len());
    for row in &rows {
        let mut typed_row: Vec<Cell> = Vec::with_capacity(columns.len());
        for (col_idx, col) in row.columns().iter().enumerate() {
            let cell = pg_column_to_cell(row, col_idx, col.type_());
            typed_row.push(cell);
        }
        typed_rows.push(typed_row);
    }

    Ok(OracleOutcome::Table(ResultTable {
        columns,
        rows: typed_rows,
    }))
}

/// Convert a single Postgres column in `row` at `col_idx` to a [`Cell`].
fn pg_column_to_cell(row: &tokio_postgres::Row, col_idx: usize, ty: &Type) -> Cell {
    match *ty {
        Type::INT2 => row
            .try_get::<_, Option<i16>>(col_idx)
            .ok()
            .flatten()
            .map_or(Cell::Null, |v| Cell::Integer(i64::from(v))),
        Type::INT4 => row
            .try_get::<_, Option<i32>>(col_idx)
            .ok()
            .flatten()
            .map_or(Cell::Null, |v| Cell::Integer(i64::from(v))),
        Type::INT8 => row
            .try_get::<_, Option<i64>>(col_idx)
            .ok()
            .flatten()
            .map_or(Cell::Null, Cell::Integer),
        Type::FLOAT4 => row
            .try_get::<_, Option<f32>>(col_idx)
            .ok()
            .flatten()
            .map_or(Cell::Null, |v| Cell::Float(f64::from(v))),
        Type::FLOAT8 => row
            .try_get::<_, Option<f64>>(col_idx)
            .ok()
            .flatten()
            .map_or(Cell::Null, Cell::Float),
        Type::NUMERIC => {
            // tokio-postgres doesn't natively parse NUMERIC to f64; read as String
            // then parse.
            if let Ok(Some(s)) = row.try_get::<_, Option<String>>(col_idx) {
                s.parse::<f64>()
                    .map_or_else(|_| Cell::Text(s), Cell::Float)
            } else {
                Cell::Null
            }
        }
        _ => {
            // For all other types (VARCHAR, TEXT, BOOL, DATE, TIMESTAMP, …),
            // stringify via try_get::<_, Option<String>>.
            row.try_get::<_, Option<String>>(col_idx)
                .ok()
                .flatten()
                .map_or(Cell::Null, Cell::Text)
        }
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing,
    clippy::float_cmp,
    clippy::approx_constant
)]
mod tests {
    use super::*;
    use crate::types::{Cell, OracleOutcome};

    #[test]
    fn substitute_placeholders_replaces_both() {
        let sql = "SELECT * FROM ${CatalogName}.${ModelName}.some_table";
        let result = substitute_placeholders(sql, "my_catalog", "my_model");
        assert_eq!(result, "SELECT * FROM my_catalog.my_model.some_table");
    }

    #[test]
    fn substitute_placeholders_noop_when_no_placeholders() {
        let sql = "SELECT 1";
        let result = substitute_placeholders(sql, "cat", "mod");
        assert_eq!(result, "SELECT 1");
    }

    #[test]
    fn oversize_fields_are_correct() {
        let outcome = OracleOutcome::Oversize {
            observed_at_least: 50_001,
            cap: 50_000,
        };
        let OracleOutcome::Oversize { observed_at_least, cap } = outcome else {
            panic!("expected Oversize");
        };
        assert!(observed_at_least > cap);
    }

    #[test]
    fn cell_integer_roundtrip() {
        let cell = Cell::Integer(42);
        let json = serde_json::to_string(&cell).unwrap();
        let back: Cell = serde_json::from_str(&json).unwrap();
        assert_eq!(cell, back);
    }

    #[test]
    fn cell_float_roundtrip() {
        // Use a value that round-trips exactly in IEEE 754 + JSON.
        let cell = Cell::Float(1.5_f64);
        let json = serde_json::to_string(&cell).unwrap();
        let back: Cell = serde_json::from_str(&json).unwrap();
        let Cell::Float(v) = back else {
            panic!("expected Float");
        };
        assert!((v - 1.5_f64).abs() < f64::EPSILON);
    }

    #[test]
    fn cell_null_roundtrip() {
        let cell = Cell::Null;
        let json = serde_json::to_string(&cell).unwrap();
        let back: Cell = serde_json::from_str(&json).unwrap();
        assert_eq!(cell, back);
    }

    #[test]
    fn sql_queries_file_parses() {
        let yaml = r#"
context: "tpcds test corpus"
queries:
  - id: "q1"
    nl_query: "What is revenue?"
    expected_sql: "SELECT SUM(revenue) FROM ${CatalogName}.${ModelName}.sales"
  - id: "q2"
    nl_query: "How many stores?"
    expected_sql: "SELECT COUNT(*) FROM ${CatalogName}.${ModelName}.store"
    disabled: true
"#;
        let file: crate::types::SqlQueriesFile =
            serde_yaml::from_str(yaml).unwrap();
        assert_eq!(file.queries.len(), 2);
        assert_eq!(file.queries[0].id, "q1");
        assert!(file.queries[1].disabled);
        assert_eq!(file.context.as_deref(), Some("tpcds test corpus"));
    }

    #[test]
    fn hard_row_ceil_is_200k() {
        assert_eq!(HARD_ROW_CEIL, 200_000);
    }
}
