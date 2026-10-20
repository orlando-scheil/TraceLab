# Tool usage in linked subagent families

Compare Claude and Codex separately, with main and descendant sessions side by side.
This analysis answers five questions:

1. How many calls does each tool receive in each role?
2. How does the tool mix differ between main and descendant sessions?
3. For the same named tool, what fraction of known outcomes are errors?
4. How many distinct named tools does a session use, including sessions with no calls?
5. Which tools account for additive positive effective tool time?

## Population and definitions

The shared `subagent_cohorts.COHORT_SQL` selects positively known delegating main
sessions with resolved self-roots and their resolved descendants. A main is counted
once regardless of its number of children. Descendants include deeper descendants,
not just direct children. Unknown-origin families, unresolved links, and mains
without linked descendants are excluded. These are matched families, not all
sessions and not a controlled comparison of equivalent tasks.

Sessions join rounds on **provider and session ID**. Calls join rounds on
**round_pk**. Calls are not expanded by child count. Tool names are compared exactly
as stored in the sanitized input: no case folding, MCP merging, or inferred aliases.
Null and whitespace-only names form an unnamed-call bucket for call and time
accounting, but never count as a distinct named tool. Other names are not trimmed.

- **Calls:** all recorded calls for a provider, role, and exact tool name.
- **Call share:** tool calls / all calls in the same provider and role, including
  unnamed calls. A role with no calls has an unavailable share, not zero percent.
- **Error rate:** `TRUE / (TRUE + FALSE)` for that tool, provider, and role.
  Unknown outcomes are excluded from this denominator, never treated as success.
- **Known-outcome coverage:** `(TRUE + FALSE) / all calls`; unknown counts appear
  alongside it. No known outcomes means an unavailable error rate.
- **Tool diversity:** distinct nonblank names per session. Sessions without calls
  or with only unnamed calls contribute a genuine zero. The table gives session
  count, mean, p25, p50, p90, and p99 using the shared `summarize` method. An empty
  cohort has count zero and unavailable statistics, not measured zero diversity.
- **Effective tool time:** the existing `trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL`
  selects internal latency when present, otherwise wall latency. A zero or negative
  internal value does **not** fall back to wall time. Only positive effective values
  are summed. Positive, missing, and nonpositive call counts are reported separately.
  With no positive observations the sum is unavailable. Time share divides a tool's
  positive sum by the positive sum for all tools in its provider and role; a tool
  with no positive observation contributes zero share only when that denominator
  exists. Overlapping calls are added, so this is **not elapsed runtime** or savings.

## Figure and full tables

The figure uses the existing shared Matplotlib style and save helper. Claude and
Codex occupy separate rows; blue means main and green means descendant in both.
Call-share and summed-time panels use the same set of up to 12 names, ranked by
combined call count across both providers and roles, with exact-name tie breaks.
“Shared” means a common display list, not tools required to occur in every group.
An unnamed-call bucket can appear in that list. Long display labels are shortened;
full names remain in the tables and CSV. Time panels use a common hours scale.

The figure names the omitted call tail; the Markdown breaks it down by provider
and role. Neither the full tool tables nor the CSV drops the tail. The call mix is
not renormalized to the displayed names. Empty provider panels say “No linked
data”; linked roles without calls and roles without positive latency are labeled
separately. Missing latency is not drawn as measured zero.

Exact-name comparisons do not imply equivalent tool behavior between providers.
Different task mixes, tool availability, family sizes, outcome recording, and
latency coverage limit comparisons. Counts and time are pooled within each role,
not equally weighted per family or session. No observed findings are assumed here.

## Commands and outputs

From the repository root, using an approved sanitized database:

```sh
uv run --no-sync python artifacts/subagents/tool_usage/plot.py --db /path/to/approved.duckdb
uv run --no-sync python artifacts/subagents/tool_usage/plot.py --db /path/to/approved.duckdb --output-dir /path/to/output
```

The house `--input /path/to/normalized.jsonl` option is also supported. The default
output directory is this experiment's directory. The script writes only:

- `tool_usage.png` — provider rows with main/descendant call mix and additive time.
- `tool_usage.md` — role outcome coverage, session diversity, omitted tail, and all
  tool rows for both providers.
- `tool_usage.csv` — aggregate `role` and `tool` rows, with no session identifiers.
  `diversity_*` fields apply only to role rows; blank tool names identify unnamed
  call buckets on tool rows. Empty numeric cells mean unavailable/not applicable.

The PNG embeds this README, plotting/shared utility source, and **only**
`tool_usage.csv`, using the existing PNG sidecar helper. It does not attach raw
traces, session rows, or unrelated CSV files from the output directory. Inputs and
tool names must already be approved for publication; this script is not a sanitizer.

Assigned synthetic validation (no corpus required):

```sh
uv run --no-sync --with pytest python -m pytest -q scripts/tests/test_subagent_tools.py
```
