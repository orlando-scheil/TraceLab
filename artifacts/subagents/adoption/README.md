# Subagent adoption and observed family structure

Six questions, always reported separately for **Claude** and **Codex**:

1. What fraction of eligible main sessions have an observed resolved descendant (`F/M`)? How many known mains are eligible (`M/M0`)?
2. What fraction of origin-classified sessions are subagents (`C0/(M0+C0)`), and how much origin evidence is unknown?
3. How many observed descendants does each delegating main have?
4. How many **known direct children** does each delegating main have, and how many descendant parent edges are known within the family?
5. Which observed families are nested, flat, or unknown, and what is `nested/(nested+flat)`?
6. What is the distribution of known descendant depths, and what fraction of matched descendants have known depth?

## Definitions and denominators

The unit of identity is an observed valid `(provider, session_id)` in `sessions`, not a round or a tool call. Scope validation and relationship resolution belong to `trace_db`; this script only reads their results.

| Symbol | Definition |
| :-- | :-- |
| `M0` | Observed sessions with canonical origin `claude_top_level_layout` or `codex_top_level_source`, including those whose linkage is ineligible. |
| `C0` | Observed sessions with canonical origin `claude_subagent_layout` or `codex_subagent_source`, regardless of whether they link to an eligible main. |
| `U` | Observed sessions with canonical origin `unknown`. Unknown-origin share is `U/(M0+C0+U)`. Origin classification is not inferred from incoming references. |
| `M` | Members of `M0` whose `root_status` is `resolved_explicit` or `resolved_chain` and whose resolved root is themselves. |
| `F` | Members of `M` with at least one observed resolved non-self descendant. |
| `D` | Observed sessions with `root_status IN ('resolved_explicit', 'resolved_chain')`, a non-self resolved root, and that root in `F`. Descendants need not have a known origin themselves. |

`artifacts/utils/subagent_cohorts.py::COHORT_SQL` supplies `F` mains once each plus `D` descendants, with columns `provider, session_id, root_session_id, role`. Root anchors of unknown origin are **not** mains. The tables show all resolved non-self descendants, how many enter matched families, and how many are excluded. In particular, Claude can have resolved membership to an unknown-origin anchor without that anchor being an eligible main; Codex may leave such membership unresolved. Unknown-origin and unavailable-linkage counts remain visible rather than being promoted into the cohort.

Each descendant-count and direct-child-count distribution has **one observation per F main**, not one per child. A known direct child has `parent_status = 'resolved'` and the observed parent equals its matched root. A zero in the direct-child distribution means **zero known direct children**, not proof of zero real children. Edge coverage counts resolved parent edges to another member of the same matched family, divided by `D`; a resolved edge to an unclassified/outside-family ancestor does not qualify.

- **Nested:** at least one validated descendant-to-descendant edge within the matched family, even if other edges or all depths are unknown.
- **Flat:** every matched descendant has a resolved direct edge to the root.
- **Unknown:** neither condition holds. Root-only membership cannot establish flatness.
- The nesting rate excludes unknown topology: `nested/(nested+flat)`. Known-topology coverage is `(nested+flat)/F`; all three family counts are shown.
- Depth is the resolver's known distance from the root (root depth 0); only descendant depths enter the distribution. Coverage is known-depth descendants divided by `D`, not by `F`. Root-only membership never supplies an invented depth.

Distributions show `N`, mean, and NumPy's linearly interpolated P25/P50/P90/P99. Empty distributions and zero-denominator rates are **N/A**; genuine zero observations and zero-numerator rates are retained. No request state machine or token measurement is used.

## Limitations

These are **observed capture** statistics, not complete real-execution adoption. An eligible main without an observed descendant may still have delegated outside the retained capture. Unknown origins, missing parents/roots, conflicts, invalid links, and cycles can reduce eligibility or topology coverage. Flat means flat among matched observed descendants only. Provider capture conventions can differ; the denominator and coverage tables are essential when comparing providers.

Unscoped normalized traces retain origin classification when available, but linkage is unavailable, so `M` and `F` are empty and adoption is N/A. Legacy DBs without `sessions` use the shared read-only compatibility view: origins are unknown and linkage unavailable, never fabricated. No raw identifiers or content are exported. This README makes no claims about real-corpus findings.

## Run

From the repository root, using an existing DB:

```bash
uv run --no-sync python artifacts/subagents/adoption/analyze.py --db trace/syfi_coding_trace.duckdb
```

Or materialize a normalized JSONL override through the shared CLI and choose an output directory:

```bash
uv run --no-sync python artifacts/subagents/adoption/analyze.py --input path/to/normalized.jsonl --output-dir /tmp/tracelab-adoption
```

The shared CLI opens the DB read-only and closes it after collection. `--input` uses the shared temporary materialization cache; it does not update the source DB. The script emits **only** `adoption.md` and `headline.json` in `--output-dir` (default: this directory). The headline is the house list of `{label, value}` objects, with explicit provider labels for `F/M` and mean descendants per F main. Markdown includes both provider sections and all denominators; stdout mirrors it.

Focused synthetic validation (also smoke-tests both scripts' `main(argv=None)` entry points):

```bash
uv run --no-sync --with pytest python -m pytest -q scripts/tests/test_subagent_counts.py
```
