# mqo-eval buildloop ledger

## Run — 20260617T190020Z → 20260617T205231Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260617T205231Z-tpcds_sq.json
- Note: Cycle 1 baseline (8→10 correct after Cycle 1 PRDs deployed)

```
pass rate: 10/20 (50%)
correct:     10
wrong:       10
no_bind:     0
parse_error: 0
skipped:     1
```

- Hypotheses: ORDER BY timeout (projection queries hang 300s), string member filter undercount (able-manufacturer-brands), spurious Rank column rejections (RULE6 gap), label alias mismatches (2 cases), channel scope inflation (store-quantity)


## Run — 20260618T021030Z → 20260618T024703Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T021030Z-tpcds_sq.json

```
pass rate:   0/20 (0%)
wrong:       20
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.000
mean_jaccard: 0.000
```

- Hypotheses:
  - Infrastructure: Cycle 2 0% result is a FALSE REGRESSION — eval ran in fixture mode (MQO_ENDPOINT missing), agent returned anonymized data vs live oracle; fixed in script before Cycle 3
  - Actual Cycle 2 changes untested: projection-orderby, string-member, rank guard, label alias, channel-scope — need live eval to measure
  - Next: re-run eval with live endpoint; expect improvement from ORDER BY fix (3 timeout cases) and label alias resolution

## Run — 20260618T030142Z → 20260618T035408Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T030142Z-tpcds_sq.json

```
pass rate:   9/20 (45%)
wrong:       11
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.745
mean_jaccard: 0.734
```

- Hypotheses:
  - Regressions (C1→C2): 6 queries that passed C1 now timeout (4) or return wrong data (2). Likely root: ORDER BY optimization changes introduced a performance cliff on complex aggregations (quantity-sold-per-year, product-count-per-category, store-returns-per-product, total-net-profit-per-product). Customer filter cases (income-band-9, new-jersey) regressed from timeout→wrong-data, suggesting schema binding changed adversarially.
  - Improvements: 2 wins (midway-stores: column alias resolved; web-sales-per-customer-state: Rank column no longer injected). Label alias and Rank-guard fixes worked on simpler queries.
  - Still failing (4 cases): able-manufacturer-brands (member-filter undercount: 188/246 brands), store-quantity-sold-per-brand (0/20 rows — likely scope mismatch on Sales table), net-profit-tier-by-store-gender-2002 (extra Net Profit column; 200/220 rows), customers-ese-store-2001 (still timeout). Underlying patterns: domain-scoped member filters, multi-table aggregation ambiguity, join path explosion.
  - Cycle 3 next: Revert ORDER BY optimization (perf regression floor), profile member-filter join cost, test attribute-only queries without aggregation ORDER BY to isolate timeouts from domain-scope issues.

## Run — 20260618T041736Z → 20260618T050830Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T041737Z-tpcds_sq.json

```
pass rate:   10/20 (50%)
wrong:       10
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.863
mean_jaccard: 0.846
```

- Hypotheses:
  - Stable at 50% (10 correct); C3→C4 regressions: store-returns-per-product (Rank column re-injected despite RULE6 fix) and slight timeout regression (5 vs 4 in C3). Member-filter undercounting persists (able-manufacturer-brands: 188/246).
  - Critical blockers: (1) multi-table aggregation timeouts (product-count, store-employee, total-net-profit, customers-ese, customer-details) point to query complexity vs ORDER BY optimization; (2) store-quantity-sold-per-brand showing 20×wrong magnitude (joined source mismatch: candidate rows 5M vs reference 170M); (3) customer-vehicle-count-income-band-9 at 50% recall suggests domain-scoped predicate not filtering (10 rows returned instead of 20).
  - Next: Profile member-filter cost on able-manufacturer case; test attribute-only/non-aggregated filters (income-band, state) to isolate predicate evaluation from ORDER BY. Investigate store-quantity multi-table join cost (Store Sales via Brand Key path) vs reference oracle.

## Run — 20260618T055300Z → 20260618T063658Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T055300Z-tpcds_sq.json

```
pass rate:   11/20 (55%)
wrong:       9
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.837
mean_jaccard: 0.833
```

- Hypotheses:
  - Timeout floor (4/9 fails): Complex multi-table aggregations timeout consistently (store-employee, total-net-profit, tier-by-store-gender, customers-ese). Suggests ORDER BY optimization from C2 is still active and adds pathological cost to queries with 3+ joins + GROUP BY + ORDER BY.
  - Member filter undercount (able-manufacturer-brands 76.4% recall): 58 brand rows missing (188/246). Predicate evaluation works but join selectivity is wrong — either materialization drops rows or domain-scope join path is incomplete.
  - Data quantity mismatch (store-quantity-sold-per-brand 0 recall): Candidate returns 214M vs reference 170M per brand (1.26x scale). Indicates join cardinality explosion or wrong table binding (Sales vs Store-Sales confusion on Brand Key path).

## Run — 20260618T072040Z → 20260618T080629Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T072040Z-tpcds_sq.json

```
pass rate:   10/20 (50%)
wrong:       10
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.817
mean_jaccard: 0.812
```

- Hypotheses:
  - Order-by suppression too coarse (C5 root cause): suppressing all ORDER BY for measure-bearing+TOPN fixed store-employee-counts engine error (j=None→0.428) but regressed store-returns-per-product (j=0.904 correct→wrong): dimension tie-breaker determines which rows land in top-N when measure values tie at cutoff. Fix: keep measure-typed ORDER BY keys, drop only dimension-typed keys.
  - Variance swings dominate (3 regressions, 2 gains): midway-stores (j=1.0 wrong — column mismatch), product-count-per-category (j=0.909), store-returns-per-product all appear agent-driven; web-sales and net-profit-tier gains are also variance. Compiler signal is noise-floored at ±1.
  - Still stuck: total-net-profit-per-product j=None (XMLA timeout on large item-level aggregation, not ORDER BY bug); customer-details-new-jersey, customers-ese-store-2001 (projection timeouts at 300s).

## Run — 20260618T083130Z → 20260618T092012Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T083130Z-tpcds_sq.json

```
pass rate:   11/20 (55%)
wrong:       9
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.784
mean_jaccard: 0.784
```

- Hypotheses:
  - ORDER BY conclusion: any ORDER BY after TOPN fails XMLA engine (measure-only refs also rejected, confirmed C6). v0.17.1 complete suppression is correct; v0.17.2 refinement was wrong. Need to revert to v0.17.1 for C7.
  - midway-stores root cause found: column_jaccard=0.5 — agent binds "Floor Space" level (wrong) vs reference "Store Floor Space" level (correct); 23 rows match exactly but label mismatch fails the eval. Fix: level disambiguation or column label normalization in the binder.
  - total-net-profit-per-product unblocked: j=None→0.0 in C6, meaning XMLA now executes the query but returns wrong data. A different issue from engine error — may be measure computation path.
  - Next dream: (1) midway-stores column label fix (high impact: j=1.0 rows + column fix = instant pass); (2) investigate store-quantity-sold-per-brand j=0.0 stable zero (wrong table binding).

## Run — 20260618T095557Z → 20260618T103727Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T095557Z-tpcds_sq.json

```
pass rate:   8/20 (40%)
wrong:       11
no_bind:     1
parse_error: 0
skipped:     1
mean_recall: 0.905
mean_jaccard: 0.905
```

- Hypotheses:
  - Column label mapping (4 cases: midway-stores, fairview-warehouses, store-employee-counts, web-sales-per-customer-state): exact row data but Jaccard fails on level/column-name mismatches ("Floor Space"→"Store Floor Space", "Number of Employees"→"Store Number of Employees"). Fix: add label canonicalization or level disambiguation in the binder.
  - Spurious Rank injection (2 cases: store-quantity-sold-per-brand, web-sales-per-customer-state): agent emits unsolicited "Rank" column; store-quantity-sold also shows 1.26x magnitude (214M vs 170M rows) suggesting wrong table binding (Sales vs Store-Sales cardinality explosion).
  - Multi-table aggregation timeout floor (4 cases: store-returns-per-product, total-net-profit-per-product, customer-vehicle-count-income-band-9, customers-ese-store-2001 all timeout 300s): timeouts cluster on queries with 3+ joins + multi-table ORDER BY; root likely not ORDER BY suppression (C7 fix was clean) but join path explosion or predicate selectivity bleedthrough.

## Run — 20260618T110729Z → 20260618T114724Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T110729Z-tpcds_sq.json

```
pass rate:   12/20 (60%)
wrong:       8
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.848
mean_jaccard: 0.845
```

- Hypotheses:
  - Timeout floor (4/8 fails): Complex multi-table projection queries with ORDER BY timeout at 300s (customer-vehicle-count-income-band-9, net-profit-tier-by-store-gender-2002, customers-ese-store-2001, customer-details-new-jersey). All involve 3+ joins + domain-scoped predicates (income band, state, gender, year).
  - Spurious columns + magnitude mismatch (2/8 fails): store-returns-per-product has extra Rank+Item ID columns (column_jaccard=0.2, rows correct but structure wrong); store-quantity-sold-per-brand shows 1.26x magnitude inflation (214M vs 170M) with spurious Rank, indicating wrong table binding (Sales vs Store-Sales cardinality bleed).
  - Member-filter undercount persists (1/8 fails): able-manufacturer-brands at 76% recall (188/246 brands), suggesting incomplete join path or predicate selectivity drop on domain-scoped brand materialization.
  - Measure sign flipped (1/8 fails): total-net-profit-per-product returns positive values vs reference negative (ordering is inverted); rows zero-match, root likely measure computation or aggregation scope.

## Run — 20260618T120451Z → 20260618T124154Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T120451Z-tpcds_sq.json

```
pass rate:   9/20 (45%)
wrong:       11
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.735
mean_jaccard: 0.735
```

- Hypotheses: (1) RULE 6 v0.9.3 bracket fix not resolving Rank — likely because catalog has a level named 'Rank' making it appear grounded; need RULE 6 to detect synthetic vs catalog Rank (e.g. Rank-in-TOPN context); (2) 3 regressions appear to be k=1 LLM variance not code regressions — fairview-warehouses column alias drift, product-count row count, web-sales Rank; (3) DREAM C10 on: Rank disambiguation (catalog Rank vs synthetic Rank in TOPN context) + column alias correction rules for Warehouse Square Footage vs Feet pattern

## Run — 20260618T131547Z → 20260618T140449Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T131547Z-tpcds_sq.json

```
pass rate:   9/20 (45%)
wrong:       11
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.786
mean_jaccard: 0.784
```

- Hypotheses: (1) v0.10.0 RULE6/10/11 showed no net gain — validator rules help when LLM reformulates correctly, but k=1 variance dominates; (2) corpus ceiling appears ~45-60% with current approach; (3) next step: multi-shot analysis (read all run JSONs, identify consistently-failing vs variance-failing cases) before dreaming more rules

## Run — 20260618T234630Z → 20260619T014918Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260618T234631Z-tpcds_sq.json
- Gate: k=3, min_pass_reps=2 (majority), skip_stable=3

```
capability pass rate: 10/20 (50%) — 13 tested, 7 carried
gate:        k=3, min_pass_reps=2
wrong:       10
no_bind:     0
parse_error: 0
skipped:     1
carried:     7
mean_recall: 0.560
mean_jaccard: 0.560
unstable:    4 case(s) with mixed reps (next-build targets):
  midway-stores [+--] FAIL
  able-manufacturer-brands [-++] PASS
  store-employee-counts [+--] FAIL
  web-sales-per-customer-state [-++] PASS
```

### Unstable cases (next-DREAM targets)
```
unstable cases (4/20):
  midway-stores [+--] FAIL
  able-manufacturer-brands [-++] PASS
  store-employee-counts [+--] FAIL
  web-sales-per-customer-state [-++] PASS
```

- Hypotheses: (pending — Haiku digest)

## Run — 20260619T022058Z → 20260619T031726Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260619T022059Z-tpcds_sq.json
- Gate: k=3, min_pass_reps=2 (majority), skip_stable=3

```
capability pass rate: 7/20 (35%)
gate:        k=3, min_pass_reps=2
wrong:       13
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.991
mean_jaccard: 0.991
unstable:    3 case(s) with mixed reps (next-build targets):
  midway-stores [+-+] PASS
  product-count-per-category [--+] FAIL
  able-manufacturer-brands [++-] PASS
```

### Unstable cases (next-DREAM targets)
```
unstable cases (3/20):
  midway-stores [+-+] PASS
  product-count-per-category [--+] FAIL
  able-manufacturer-brands [++-] PASS
```

- Hypotheses: (pending — Haiku digest)

## Run — 20260619T041540Z → 20260619T064400Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260619T041540Z-tpcds_sq.json
- Gate: k=3, min_pass_reps=2 (majority), skip_stable=3

```
capability pass rate: 10/20 (50%)
gate:        k=3, min_pass_reps=2
wrong:       10
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.848
mean_jaccard: 0.842
unstable:    4 case(s) with mixed reps (next-build targets):
  total-quantity-sold-per-year [+--] FAIL
  able-manufacturer-brands [++-] PASS
  total-net-profit-per-product [-+-] FAIL
  net-profit-tier-by-store-gender-2002 [--+] FAIL
```

### Unstable cases (next-DREAM targets)
```
unstable cases (4/20):
  total-quantity-sold-per-year [+--] FAIL
  able-manufacturer-brands [++-] PASS
  total-net-profit-per-product [-+-] FAIL
  net-profit-tier-by-store-gender-2002 [--+] FAIL
```

- Hypotheses: (pending — Haiku digest)

## Run — 20260619T062043Z → 20260619T064753Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/mcp-aws-live/tpcds_sql_derived_limited/20260619T062043Z-tpcds_sq.json
- Gate: k=3, min_pass_reps=2 (majority), skip_stable=3

```
capability pass rate: 2/20 (10%)
gate:        k=3, min_pass_reps=2
wrong:       18
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 1.000
mean_jaccard: 1.000
unstable:    1 case(s) with mixed reps (next-build targets):
  midway-stores [-++] PASS
```

### Unstable cases (next-DREAM targets)
```
unstable cases (1/20):
  midway-stores [-++] PASS
```

- Hypotheses: (pending — Haiku digest)

## Run — 20260619T193933Z → 20260619T221938Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/docker-local/tpcds_sql_derived_limited/20260619T193933Z-tpcds_sq.json
- Gate: k=1

```
pass rate: 5/20 (25%)
wrong:       14
no_bind:     1
parse_error: 0
skipped:     1
mean_recall: 0.952
mean_jaccard: 0.952
```

### Unstable cases (next-DREAM targets)
```
unstable cases: none (all unanimous)
```

- Hypotheses: (pending — Haiku digest)

## Run — 20260620T023610Z → 20260620T031938Z
- Record: /home/jsy/projects/mqo-eval/buildloop/results/claude-oauth/docker-local/tpcds_sql_derived_limited/20260620T023610Z-tpcds_sq.json
- Gate: k=1

```
pass rate: 14/20 (70%)
wrong:       6
no_bind:     0
parse_error: 0
skipped:     1
mean_recall: 0.817
mean_jaccard: 0.812
```

### Unstable cases (next-DREAM targets)
```
unstable cases: none (all unanimous)
```

- Hypotheses: (pending — Haiku digest)
