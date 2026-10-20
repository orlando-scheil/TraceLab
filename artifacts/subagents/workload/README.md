# Matched main and subagent workload

Six questions, separately for **Claude** and **Codex**:

1. How many retained LLM rounds occur per session?
2. How many observed tool calls occur per session, including sessions with zero calls?
3. How do those distributions compare between delegating mains and their linked descendants?
4. What fraction of sessions have zero observed tool calls?
5. What fraction have exactly one observed round (not a success or completion measure)?
6. How many tool calls occur per retained round, including zero-tool rounds?

## Cohorts and denominators

The shared `artifacts/utils/subagent_cohorts.py::COHORT_SQL` returns `provider, session_id, root_session_id, role`:

- **Main (`F`):** positive known main origin (`claude_top_level_layout` or `codex_top_level_source`), resolved self-root (`root_status IN ('resolved_explicit', 'resolved_chain')`), and at least one observed resolved non-self descendant.
- **Descendant (`D`):** a session whose root status is `resolved_explicit` or `resolved_chain`, whose resolved root differs from itself, and whose root is one of those mains. A descendant need not have a known origin itself. Unknown-origin root-anchor families and unrelated mains are excluded.

Each delegating main contributes **once**, no matter how many descendants it has. Descendant distributions are session-weighted over all linked descendants, not equally weighted family averages or paired per-family differences. Both roles are reported within each provider; no merged-only headline substitutes for the provider comparisons.

| Metric | Observations / denominator |
| :-- | :-- |
| Retained rounds/session | One count of retained `rounds` rows per cohort session. |
| Tool calls/session | One count of observed `tool_calls` rows per cohort session; zero is included. |
| Zero-tool share | Cohort sessions with zero observed tool rows / all sessions in that same provider and role. |
| One-observed-round share | Cohort sessions with exactly one retained round / all sessions in that same provider and role. |
| Tool calls/round | One count per retained round in the cohort, including zero-tool rounds. The mean equals total tools / total rounds, **not** the mean of session-level ratios. |

`round_pk` is the retained ingestion identity. Duplicate `round_id`, `trace_key`, and `(session_id, round_index)` values remain separate observations. Tools are aggregated by `round_pk` **before** joining rounds to avoid multiplying round counts by tool fanout. No request/turn state machine or completion filter is used. Session population is the observed valid `(provider, session_id)` identity from `sessions`.

Distributions report `N`, mean, and NumPy's linearly interpolated P25/P50/P90/P99. Empty distributions or zero-denominator shares are **N/A**; an observed tool count of zero stays zero. The tables show observed sessions, matched mains/descendants, sessions outside matched cohorts, all resolved descendants, excluded resolved descendants, and unavailable linkage. These distinguish no cohort evidence from zero observed workload. Unscoped traces and legacy DBs without `sessions` work through the shared DB interface but have no matched cohorts while linkage is unavailable.

## Limitations

The measurements describe **retained capture**, not complete real execution. Zero tool rows means no calls observed in that capture, not proof no tools were used. One observed round is not success, completion, a single user request, or evidence that a task was simple. Missing descendants or truncated sessions can bias workload comparisons. Unknown origins, unresolved links, and provider-specific capture conventions affect who enters the cohort; the separate adoption experiment reports eligibility and classification denominators in detail.

No raw session identifiers/content, token counts, or inferred request counts are exported. This README reports definitions, not invented real-corpus findings.

## Run

From the repository root, using an existing DB:

```bash
uv run --no-sync python artifacts/subagents/workload/analyze.py --db trace/syfi_coding_trace.duckdb
```

Or use the shared normalized-JSONL override and choose an output directory:

```bash
uv run --no-sync python artifacts/subagents/workload/analyze.py --input path/to/normalized.jsonl --output-dir /tmp/tracelab-workload
```

The shared CLI opens the DB read-only; the connection is closed after collection. `--input` uses the existing temporary materialization cache, without changing the source DB. The script emits **only** `workload.md` and `headline.json` in `--output-dir` (default: this directory). Headlines use the house list of `{label, value}` objects and explicitly label the provider and role for mean rounds/session. Markdown and stdout show both providers, both roles, denominators, and coverage.

Focused synthetic validation (including output smoke checks for both scripts):

```bash
uv run --no-sync --with pytest python -m pytest -q scripts/tests/test_subagent_counts.py
```
