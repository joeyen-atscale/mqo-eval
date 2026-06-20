#!/usr/bin/env python3
"""Pre-mint gold answers for all corpus cases and write a cache JSON.

Usage:
    uv run python scripts/mint-gold.py \
        --corpus corpus/tpcds_sql_derived_limited.yaml \
        --out corpus/gold_docker-local.json \
        --pg-host localhost --pg-port 15432 \
        --pg-user admin --pg-pass-env ATSCALE_PG_PASS \
        --pg-dbname tpcds_main --sslmode disable \
        --catalog-name '"atscale_catalogs"."tpcds_main"' \
        --model-name '"tpcds_benchmark_model"' \
        --timeout 180

Output format (one entry per case_id):
    {
      "<case_id>": {"columns": [...], "rows": [[...], ...]},
      "<case_id>": {"error": "<message>"},
      ...
    }
"""

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path

import yaml

PLACEHOLDER_RE = re.compile(r"\$\{(CatalogName|ModelName)\}")


def substitute(sql: str, catalog: str, model: str) -> str:
    def _r(m: re.Match) -> str:
        return catalog if m.group(1) == "CatalogName" else model
    return PLACEHOLDER_RE.sub(_r, sql)


def run_query_with_timeout(
    host: str, port: int, user: str, password: str, dbname: str,
    sslmode: str, sql: str, timeout_s: int,
) -> tuple[list[str], list[list]] | str:
    """Returns (columns, rows) or an error string. Hard timeout via thread."""
    import psycopg2

    result_holder: list = [None]
    error_holder: list = [None]

    def _run() -> None:
        try:
            conn = psycopg2.connect(
                host=host, port=port, user=user, password=password,
                dbname=dbname, sslmode=sslmode, connect_timeout=10,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cols = [d[0] for d in (cur.description or [])]
                    rows = cur.fetchall()
                    result_holder[0] = (cols, [[_cell(c) for c in r] for r in rows])
            finally:
                conn.close()
        except Exception as exc:
            error_holder[0] = str(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_s)

    if t.is_alive():
        return f"timed out after {timeout_s}s"
    if error_holder[0]:
        return error_holder[0]
    return result_holder[0]  # type: ignore[return-value]


def _cell(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        import decimal
        if isinstance(v, decimal.Decimal):
            return float(v)
    except ImportError:
        pass
    return str(v).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pg-host", default="localhost")
    parser.add_argument("--pg-port", type=int, default=15432)
    parser.add_argument("--pg-user", default="admin")
    parser.add_argument("--pg-pass-env", default="ATSCALE_PG_PASS")
    parser.add_argument("--pg-dbname", default="tpcds_main")
    parser.add_argument("--sslmode", default="disable")
    parser.add_argument("--catalog-name", default='"atscale_catalogs"."tpcds_main"')
    parser.add_argument("--model-name", default='"tpcds_benchmark_model"')
    parser.add_argument("--timeout", type=int, default=180, help="per-query timeout in seconds")
    args = parser.parse_args()

    password = os.environ.get(args.pg_pass_env)
    if not password:
        print(f"ERROR: env var {args.pg_pass_env!r} not set", file=sys.stderr)
        sys.exit(1)

    corpus_data = yaml.safe_load(Path(args.corpus).read_text())
    queries = corpus_data.get("queries", [])

    out_path = Path(args.out)
    # Load existing cache so we can resume
    cache: dict = {}
    if out_path.exists():
        cache = json.loads(out_path.read_text())
        print(f"Resuming: {len(cache)} entries already cached")

    total = len(queries)
    for i, q in enumerate(queries, 1):
        case_id = q["id"]
        if q.get("disabled"):
            print(f"[{i}/{total}] {case_id}: SKIPPED (disabled)")
            continue
        if case_id in cache:
            print(f"[{i}/{total}] {case_id}: already cached, skipping")
            continue

        sql = q.get("expected_sql", "")
        if not sql:
            cache[case_id] = {"error": "no expected_sql in corpus"}
            print(f"[{i}/{total}] {case_id}: no SQL")
            continue

        sql = substitute(sql.rstrip().rstrip(";"), args.catalog_name, args.model_name)
        print(f"[{i}/{total}] {case_id}: running...", end="", flush=True)

        result = run_query_with_timeout(
            host=args.pg_host, port=args.pg_port,
            user=args.pg_user, password=password,
            dbname=args.pg_dbname, sslmode=args.sslmode,
            sql=sql, timeout_s=args.timeout,
        )

        if isinstance(result, str):
            cache[case_id] = {"error": result}
            print(f" ERROR: {result}")
        else:
            cols, rows = result
            cache[case_id] = {"columns": cols, "rows": rows}
            print(f" OK ({len(rows)} rows)")

        # Write incrementally so a crash doesn't lose progress
        out_path.write_text(json.dumps(cache, indent=2))

    print(f"\nDone. {sum(1 for v in cache.values() if 'columns' in v)} OK, "
          f"{sum(1 for v in cache.values() if 'error' in v)} errors. "
          f"Written to {out_path}")


if __name__ == "__main__":
    main()
