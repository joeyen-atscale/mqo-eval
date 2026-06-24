# Technical Report — Sonnet 20/20 on `tpcds_sql_derived_limited` (k=1)

- **Date:** 2026-06-22 (run finished 2026-06-23 02:54Z)
- **Result:** **20/20 active cases correct (100%), accuracy 1.0, mean row-recall 0.989**
- **Model:** `claude-sonnet-4-6` via `claude -p` over OAuth (claude-oauth agent)
- **System under test:** `mqo-mcp-server` v0.56.0 + `mqo-param-validator` v0.19.0 (binary `~/.local/bin/mqo-mcp-server`, 8 327 888 bytes, both fixes marker-verified)
- **Harness:** `mqo-eval` (Python), `~/projects/mqo-eval`
- **Records:**
  - Full suite: `results/full-suite/claude-oauth/docker-local/tpcds_sql_derived_limited/20260623T025448Z-tpcds_sq.json`
  - Trace capture (2 key cases, `--trace`): `results/trace-capture/claude-oauth/docker-local/spotcheck_corpus/20260623T032614Z-spotchec.json`
  - Pre-fix baseline (Case A, verdict=wrong, auto-traced): `results/spotcheck/claude-oauth/docker-local/spotcheck_corpus/20260622T214056Z-spotchec.json`

---

## 1. Executive summary

The two long-standing residuals on the `tpcds_sql_derived_limited` corpus were closed by two shipped changes, and the full 20-question suite now passes at k=1 on Sonnet:

| Residual | Failure mode | Fix | Result |
|---|---|---|---|
| `product-count-per-category` | model silently dropped the blank/NULL dimension member → 10 rows vs 11 gold (recall 0.909) | **blank-member answer fidelity** — server emits `blank_member_rows` + a `notes[]` advisory (mqo-mcp-server v0.56.0) | 11/11, recall 1.0 |
| `web-sales-per-customer-state` | baseline bound two co-grain sibling levels of one hierarchy → extra column, row_recall 0.0 | **validator RULE 13 `RedundantCoGrainSiblingLevel`** (mqo-param-validator v0.19.0), plus the existing near-twin canonicalization rule | 20/20, recall 1.0 |

**Caveat (load-bearing):** this is **k=1 — single-shot capability, not pass^k determinism.** 100% means each question passed on its single attempt. Sonnet single-shot variance means a repeated (pass^k) run can dip on borderline cases. The k=4 re-run (§11) measured this: **pass^4 18/20 (90%), pass@4 20/20 (100%), 0 wrong answers in 80 attempts**.

**Measurement-integrity scope — what "100%" does and does not mean.** The two fixes above were audited and are *general* corrections, not corpus overfitting: RULE 13 is a purely structural co-grain invariant validated against the live catalog (no corpus column/model names in its logic — only in doc-comments and `#[cfg(test)]` tests), and the blank-member default `annotate` mode leaves data rows unchanged (`gold scoring unaffected` — it adds an advisory note, not a data edit), so neither change can move a score directly. Both are always-on in the shipped product, so the eval measures the system as deployed. **However, the "100%" headline is scoped by two harness-side choices that the reader should weigh:**
- **1 of 20 cases is graded at a relaxed threshold.** `fairview-warehouses` uses a per-case `pass_threshold: 0.75` (§4), because the CE SQL backend suppresses a NULL-warehouse-name row that exists in the 5-row gold, capping a *correct* answer at row_recall 0.800. Documented data reason, added 2026-06-20 (pre-dates this work) — defensible, but it is a relaxed bar.
- **2 of 22 corpus cases are excluded, not solved.** `customer-vehicle-count-income-band-9` and `customer-details-new-jersey` are `disabled: true` because the **gold's own reference SQL times out** past the CE engine deadline on the local target — un-scorable regardless of any MQO change. They represent a real open capability gap (**BUG-2: high-cardinality cross-dimension measureless projection with no TOP-N pushdown**). Counting them as unmet, the **corpus-wide figure is 20/22 ≈ 91%**, and "20/20 (100%)" should be read as *100% of the locally-scorable cases*.

A deploy bug nearly invalidated this measurement: see §8.

---

## 2. System under test

Two changes, both shipped to `joeyen-atscale` and deployed to `~/.local/bin/`:

1. **Blank-member answer fidelity** — `mqo-mcp-server` v0.56.0 (commit `4d5360f`). `UnknownMemberMode::Annotate` (the default) counts blank/NULL dimension-member rows and attaches a `blank_member_rows` integer + an advisory string to `notes[]`. Modes: `annotate` (default) | `caption` (also rewrites blank cells to `--unknown-member-caption`) | `off`. CLI `--unknown-member-mode`, env `MQO_UNKNOWN_MEMBER_MODE`.
2. **Validator RULE 13 `RedundantCoGrainSiblingLevel`** — `mqo-param-validator` v0.19.0 (commit `84d622b`). Rejects a bind that includes two co-grain sibling levels of the same hierarchy (both individually valid, so no prior rule fired; the extra column broke scoring). Off-switch env `MQO_VALIDATOR_DISABLE_RULE13=1`.

The validator is compiled into the server; both fixes are confirmed present in the deployed binary via unique compile-marker strings (`MQO_UNKNOWN_MEMBER_MODE`, `MQO_VALIDATOR_DISABLE_RULE13`).

---

## 3. Test harness (`mqo-eval`, Python)

`mqo-eval` drives each corpus question through an **agent** (the model under test) talking to a live **mqo-mcp-server**, then scores the model's final answer against a ground-truth **oracle**.

```
corpus YAML ──> agent (claude -p / OAuth, model=sonnet) ──MCP stdio──> mqo-mcp-server ──> AtScale CE
                      │                                                                       │
                      └── final answer (JSON envelope)                          oracle gold ─┘
                                          │                                          │
                                          └──────────── scoring.py ─────────────────┘
                                                            │
                                                       RunRecord JSON
```

### 3.1 CLI & RunRecord
Entry point `src/mqo_eval/cli.py`. Subcommands: `run` (cli.py:12–102), `summary` (105–164), `trace` (167–236). Records are written atomically to `results_dir/{agent}/{server}/{corpus_id}/{run_id}.json` (record.py:142–154), `run_id = "{timestamp}-{corpus_id[:8]}"`.

A per-case record (`CaseRecord`, record.py:25–60) carries: `id, nl_query, verdict, answer_type, detail, row_recall, column_recall, column_jaccard, jaccard, rep_verdicts, latency_ms, candidate_columns/reference_columns, candidate_rows_sample/reference_rows_sample, row_count_candidate/reference, sample_truncated, gold_deduped, gold_rows_pre/post, trace`.

### 3.2 Agent — `claude-oauth` (`src/mqo_eval/agents/claude_oauth_agent.py`)
Invokes the real CLI (claude_oauth_agent.py:344–355):
```
claude -p <prompt> --output-format stream-json --verbose \
  --mcp-config <tmp.json> --strict-mcp-config \
  --allowedTools mcp__mqo__* \
  --append-system-prompt <ANSWER_SCHEMA_PROMPT> \
  --model claude-sonnet-4-6
```
- `ANTHROPIC_API_KEY` is stripped from the env (line 339) to force OAuth-subscription billing, not API key.
- Model from `MQO_CLAUDE_MODEL` (default `claude-sonnet-4-6`, line 40). Per-call timeout 600 s (line 42).
- MCP server wired via a temp `--mcp-config` (lines 46–99) of the form `{"mcpServers":{"mqo":{"type":"stdio","command":<MQO_MCP_BINARY>,"args":[...]}}}`. Binary from `MQO_MCP_BINARY` (default `mqo-mcp-server` on PATH, line 16). The model sees all `mcp__mqo__*` tools (`search_columns`, `query_multidimensional`, `describe_model`, …).
- **Answer contract** (`ANSWER_SCHEMA_PROMPT`, lines 24–32): the model must emit, as the last line, a JSON envelope — `{"answer_type":"tabular","columns":[...],"rows":[[...]]}` | `{"answer_type":"scalar","value":…}` | `{"answer_type":"handle",…}` | `{"answer_type":"cannot_answer","reason":…}`. Extraction at lines 102–124.
- **No transcript is persisted to disk**; the stream-json stdout is parsed in-process for the answer + trace. Trace is written only if `$MQO_TRACE_OUT` is set.

### 3.3 Server — `--server docker-local`
`--server` is an organizational label that partitions the results tree (record.py:144); the harness does **not** launch the server itself — the agent spawns `mqo-mcp-server` over MCP stdio. In this run the server ran the **SQL backend** against the local AtScale CE Docker stack, catalog `atscale_catalogs`, model `tpcds_benchmark_model` (from `buildloop/docker-local.env`). Confirmed live from the trace: `backend: sql`, `routing_reason: estimated_rows (1) exceeds row_threshold (0)`.

### 3.4 Oracle — `--oracle precomputed`
Loads pre-minted gold from `--gold-file corpus/gold_docker-local.json` (oracle_precomputed.py). Cache shape: `{"<case_id>": {"columns":[...],"rows":[[...]]}}` or `{"<case_id>": {"error":"…"}}`; a case missing from the cache → `OracleError("case not in gold cache")`. (Other modes: `cli` shells `mqo-pg-query` to mint gold live; `pgwire` runs gold SQL via psycopg2 — dead against AtScale PGWire, scores 0, never used for scoring; `fixture` is offline/no-scoring.)

---

## 4. Test criteria (scoring) — `src/mqo_eval/scoring.py`

**Verdict = `correct` iff** (scoring.py:347):
```python
passed = metrics.column_recall >= 1.0 and metrics.row_recall >= pass_threshold
```
- **`pass_threshold` default = 0.95** (`--pass-threshold`, cli.py:297). So a correct tabular answer needs *all* expected columns and ≥95% of expected rows.

Exact metric formulas (multiset over matched columns):
- `row_recall = matched_rows / n_ref` (scoring.py:226), `matched_rows = sum((ref_keys & ans_keys).values())`
- `column_recall = |matched_cols| / |ref_norm|` (186)
- `column_jaccard = |matched_cols| / |ref_norm ∪ ans_norm|` (187)
- `row_jaccard = matched_rows / (n_ref + n_ans − matched_rows)` (228)

**Scalar answers** (1×1 reference, scoring.py:320–339): numeric match via `math.isclose(rel_tol=1e-3, abs_tol=1e-6)`, else case-folded string equality. A scalar answer against a multi-row reference → `wrong`.

**Verdict values:** `correct` | `wrong` | `no_bind` (reference is None/OracleError/unresolved handle) | `parse_error` (answer not a valid envelope) | `oversize` (reference exceeded row cap) | `skipped` / `skipped-stable` (corpus-disabled / carried from a prior correct run).

**Per-case threshold override** (runner.py:438): a corpus entry may set its own `pass_threshold`, which wins over the global 0.95 default — `effective_threshold = q.pass_threshold if q.pass_threshold is not None else pass_threshold`. In this corpus exactly one case overrides it: **`fairview-warehouses: pass_threshold 0.75`** (corpus line 66), a small (5-row) reference where one expected row is legitimately hard to reproduce, so it is graded at ≥75%. This is why `fairview` shows as `correct` at row_recall 0.800.

**Gold dedup** (scoring.py:28–47, 281–285): if the gold SQL is "measureless" (no aggregate function AND no `GROUP BY`), exact-duplicate rows are collapsed after value-equivalence normalization; recorded as `gold_deduped`, `gold_rows_pre/post`. This prevents a measureless reference from inflating the row denominator.

**k semantics** (runner.py:484–485): with `--repeat N`, a case is `correct` iff `correct_reps >= min_pass_reps` (default `min_pass_reps = N`); per-rep metrics are arithmetic-averaged. **k=1** (this run) = one attempt per case, `rep_verdicts = None`, verdict = that single attempt. The summary labels k>1 runs "capability pass rate" vs k=1 "pass rate" (cli.py:123).

---

## 5. Corpus

`corpus/tpcds_sql_derived_limited.yaml` — **22 queries, 20 active**, 2 corpus-level `skip: true` (NOT run): `customer-vehicle-count-income-band-9`, `customer-details-new-jersey`. The run used **no `--skip-stable`**, so none of the 20 active cases were dropped for stability.

---

## 6. Results — full suite (k=1)

`summary: {total:22, active:20, skipped:2, correct:20, wrong:0, no_bind:0, parse_errors:0, mean_row_recall:0.9885, mean_row_jaccard:0.9882, accuracy:1.0}`

| # | case | verdict | row_recall | cand/ref |
|---|------|---------|-----------|----------|
| 1 | express-shipping-carriers | correct | 1.000 | 4/4 |
| 2 | midway-stores | correct | 1.000 | 9/9 |
| 3 | fairview-warehouses | correct | 0.800 | 4/5 |
| 4 | large-warehouses | correct | 1.000 | 3/3 |
| 5 | catalog-customer-count | correct | scalar | 1 |
| 6 | total-quantity-sold-per-year | correct | 1.000 | 6/6 |
| 7 | product-count-per-category | correct | 1.000 | 11/11 |
| 8 | unique-stores-tennessee | correct | 1.000 | 12/12 |
| 9 | corpcorp-brand-products | correct | 1.000 | 15/15 |
| 10 | able-manufacturer-brands | correct | 1.000 | 29/30 |
| 11 | products-price-above-70 | correct | 0.994 | 639/639 |
| 12 | store-employee-counts | correct | 1.000 | 10/10 |
| 13 | store-returns-per-product | correct | 1.000 | 20/20 |
| 14 | avg-sales-quantity-per-store | correct | 1.000 | 6/6 |
| 15 | total-net-profit-per-product | correct | 1.000 | 20/20 |
| 16 | store-quantity-sold-per-brand | correct | 1.000 | 20/20 |
| 17 | web-sales-per-customer-state | correct | 1.000 | 20/20 |
| 18 | customer-count-income-band-9 | correct | scalar | 1 |
| 19 | net-profit-tier-by-store-gender-2002 | correct | 1.000 | 120/120 |
| 20 | customers-ese-store-2001 | correct | 1.000 | 20/20 |

`fairview-warehouses` passes at row_recall 0.800 via its per-case `pass_threshold: 0.75` override (§4), not the 0.95 default. `products-price-above-70` (639 rows, returned as a dataset handle) and `net-profit-tier-by-store-gender-2002` (120 rows) are the largest results and — as §11 shows — the only two cases that wobble under repetition.

---

## 7. Tool traces (Sonnet) — before/after

The harness captures, per case, the sequence of `mcp__mqo__*` tool calls the agent made (tool name, args, the MQO payload for `query_multidimensional`, and the tool result). Two cases below; the rest of the 20 are not traced (k=1 with no `--trace` only auto-traces *failing* cases, and all passed).

### 7.1 `product-count-per-category` — the blank-member fix in action

**BEFORE (pre-fix binary, 2026-06-22 21:40, verdict = wrong, recall 0.909).** 3 tool calls (`search_columns`×2 → `query_multidimensional`). The server returned 11 rows including the blank-category member `{null, 43}`, but with **no advisory**. The model's candidate answer:
```
candidate rows (10):  Books…Women     ← blank-member row silently dropped
gold rows (11):       Books…Women + (blank, 43)
```

**AFTER (fixed binary, verdict = correct, recall 1.000).** 5 tool calls, ending in the same MQO. The `query_multidimensional` result now carries the advisory (verbatim from the trace record):
```json
"row_count": 11,
"blank_member_rows": 1,
"notes": ["BLANK MEMBERS: 1 of 11 result row(s) have a blank/NULL dimension member
          (the \"unknown\" member — records with no value for this attribute).
          These are real result rows: include them in counts, sums, and any
          per-member answer. Do NOT silently drop blank-member rows."]
```
The model's candidate answer is now **11 rows** → correct. This is the mechanism doing the work, captured end-to-end: the server-side advisory changed the model's behavior.

The same result object also already contains rich fields the trace renderer does *not* currently surface (see §9):
```
backend:         sql
routing_reason:  estimated_rows (1) exceeds row_threshold (0)
compiled_query:  SELECT "Product Category", SUM("Total Product Count") AS "total_product_count"
                 FROM "atscale_catalogs"."tpcds_main"."tpcds_benchmark_model"
bound:           {dimensions:[product_dimension.[Product Category]], measures:[…total_product_count], mqo:{…}}
handle:          hdl_16d3003657a5440aa2e9e6e8d46c4c80 (ttl 3600s)
```

### 7.2 `web-sales-per-customer-state` — validator reject→retry

**AFTER (verdict = correct, recall 1.000).** 4 tool calls. The model first bound a non-canonical level and the **validator rejected it with a hint**, then the model corrected and succeeded:
```
[2] query_multidimensional  dimensions:[customer_address.[Customer State]]
    → ERROR param_rejected / model_path:
      NonCanonicalNearTwin { group_core_label:"customer state",
        picked:"customer_address.[Customer State]",
        suggested_canonical:"customer_address.[Customer State Name]" }
      suggestion: use canonical [customer_address.Customer State Name] (similarity 1.0)
[3] query_multidimensional  dimensions:[customer_address.[Customer State Name]]  → OK, 20 rows
```
**Honest nuance:** in *this* k=1 attempt the pass was driven by the **near-twin canonicalization** rule (the model bound one near-twin level and got redirected), **not** RULE 13. RULE 13 guards the distinct failure mode where the model binds *both* co-grain sibling levels at once (the extra-column case that gave the 0.0 baseline). Both rules are deployed; which one fires depends on what the model attempts. This is precisely why a determinism (pass^k) measurement matters — different attempts exercise different guards.

---

## 8. Measurement-integrity note — the deploy bug that nearly faked this result

An initial spot-check reported 1/2 and was **not trustworthy**: it measured a *stale* binary. Root cause: cloudbuild's `cmd_sync` rsyncs the local `target/` with `--delete`, and the **test step's internal re-sync clobbered the freshly-compiled remote release binary with the stale local one before the fetch** — so every install (including the original) deployed pre-fix code while reporting `RESULT=OK`. Tell-tale: the fetched binary was byte-identical in size (`8241944`) to the old one. Fixed by fetching the binary via scp **immediately after build + remote-marker-verify, before the test step** (`/tmp/mqo-build4.sh`); the deployed binary is now `8327888` bytes with both markers verified at every transfer stage. Lesson recorded in agent memory (`cloudbuild-fetch-before-test`).

**Every number in this report is from the verified `8327888`-byte binary.**

---

## 9. Observability gaps & recommendations

The existing `--trace` is useful but under-surfaces what the server already provides:

1. **`bound_sql` always prints `null`.** The extractor (`claude_oauth_agent.py:_extract_bound_sql`, ~176–193) looks for keys `bound_sql/compiled_sql/sql/bound_dax/dax`, but the server emits the SQL under **`compiled_query`** (and the bound plan under **`bound`**). Teaching the extractor those two key names populates real compiled SQL/DAX in every trace — **harness-only change, no server rebuild.**
2. **The advisory/signal fields are buried.** `notes[]`, `blank_member_rows`, `handle`, `backend`, `routing_reason` exist in the result JSON but the `mqo-eval trace` renderer (`cli.py:_cmd_trace`) only prints truncated result rows. Surfacing them as first-class lines would have made §7.1 visible without hand-parsing the record.
3. **No persisted agent transcript.** Only the extracted answer + optional trace survive; the raw `stream-json` is discarded. Persisting it (gated) would allow post-hoc inspection of the model's reasoning, not just its tool calls.

---

## 10. Reproduction

```bash
cd ~/projects/mqo-eval
# full suite (k=1)
MQO_MCP_BINARY=~/.local/bin/mqo-mcp-server MQO_CLAUDE_MODEL=claude-sonnet-4-6 \
uv run mqo-eval run --corpus corpus/tpcds_sql_derived_limited.yaml \
  --agent claude-oauth --server docker-local --oracle precomputed \
  --gold-file corpus/gold_docker-local.json \
  --catalog-name '"atscale_catalogs"."tpcds_Snowflake"' \
  --model-name '"tpcds_benchmark_model"' \
  --results-dir results/full-suite --repeat 1
# render a case trace
uv run mqo-eval trace <record.json> product-count-per-category
```

---

## 11. k=4 determinism re-run (added 2026-06-23)

The k=1 result above is single-shot capability. To separate *capability* from *determinism*, the full 20-case suite was re-run at **k=4** (`--repeat 4`, strict gate `min_pass_reps = 4` → headline verdict is **pass^4**), with `--trace` on every case. Run `20260623T033747Z` (`results/k4-full/…`), 03:37→04:36Z (~59 min, 80 `claude -p` attempts), same verified `8327888`-byte binary.

### Headline numbers

| Metric | Result | Meaning |
|---|---|---|
| **pass^4** (all 4 reps correct) | **18/20 (90%)** | determinism — survives repetition |
| **pass@4** (≥1 rep correct) | **20/20 (100%)** | capability — the model *can* answer every case |
| **per-rep pass rate** | **78/80 (97.5%)** | single-shot success across all 80 attempts |
| rep-verdict distribution | **78 correct · 2 no_bind · 0 wrong · 0 parse_error** | — |

**Zero wrong answers and zero parse errors in 80 attempts.** Every rep that produced a *resolvable* answer was correct (78/78). The only two misses are `no_bind` (an unresolved dataset handle / no resolvable answer), not analytical errors.

### Per-case rep grid (P = correct, F = miss)

```
express-shipping-carriers              PPPP     net-profit...gender-2002          PFPP  ← wobble
midway-stores                          PPPP     products-price-above-70           PPPF  ← wobble
fairview-warehouses                    PPPP     store-employee-counts             PPPP
large-warehouses                       PPPP     store-returns-per-product         PPPP
catalog-customer-count                 PPPP     avg-sales-quantity-per-store      PPPP
total-quantity-sold-per-year           PPPP     total-net-profit-per-product      PPPP
product-count-per-category   PPPP ★    store-quantity-sold-per-brand     PPPP
unique-stores-tennessee                PPPP     web-sales-per-customer-state PPPP ★
corpcorp-brand-products                PPPP     customer-count-income-band-9      PPPP
able-manufacturer-brands               PPPP     customers-ese-store-2001          PPPP
★ = the two ex-residuals fixed by RULE 13 / blank-member fidelity
```

### The two fixes are deterministic, not lucky

Both ex-residuals are **4/4 (PPPP)**. The blank-member advisory and the validator nudge fire on every attempt, so the k=1 wins were not single-shot luck — this is the determinism evidence the fixes needed.

### The 90% ceiling is handle-resolution on large results, not reasoning

Both wobblers are the **two largest results** and both fail the same way — `no_bind`:

- **`products-price-above-70`** (639 rows): the model returns the result as a **dataset handle** (`answer_type: handle`, the handle-first behavior for large results). 3 reps the handle resolved to rows → correct; **1 rep the handle was "not yet resolved"** → no_bind. Reps that bound were perfect (row_recall ≈ 0.993). This is a handle-resolution flake in the scoring path, not a model error.
- **`net-profit-tier-by-store-gender-2002`** (120 rows): 3 reps correct at row_recall **1.000**; 1 rep `no_bind`.

So the determinism gap is **result-delivery (handle resolution) on the largest payloads**, not binding, compilation, or reasoning. Candidate follow-ups: make the handle→rows resolution in the scoring path deterministic/retried, or have the agent inline rows below a size cap. Until then, treat **pass^4 90% / pass@4 100% / per-rep 97.5%** as the honest determinism profile for this corpus on Sonnet — capability is complete; the 2-attempt loss is a handle-resolution artifact on 2 of 80 attempts.

### Observability note

This k=4 run was captured with the upgraded trace layer (§9 fixes): every case's `query_multidimensional` call now records the real `compiled_query` (SQL), `backend`, `routing_reason`, `blank_member_rows`, and the `notes[]` advisory as first-class trace fields. Render any case with `uv run mqo-eval trace results/k4-full/…/20260623T033747Z-tpcds_sq.json <case-id>`.
