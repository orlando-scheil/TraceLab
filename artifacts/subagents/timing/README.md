# Subagent timing

Compare recorded timing for Claude and Codex subagents, including workflow subagents. **Observed elapsed time** runs from the earliest to the latest recorded event or valid tool endpoint, including gaps. It is a recorded span, not a verified spawn-to-return duration.

**Time with tool calls outstanding** is time when at least one tool call is awaiting a result. Overlapping calls count once. This does not measure internal tool execution, and the remaining time cannot be assumed to be model time.

This uses the same normalized trace as the other timing analyses. The tool-usage and older session-timing pages sum each call's **effective latency**: internal duration when recorded, otherwise emitted-to-result wall time. This page instead merges emitted-to-result intervals to measure elapsed time with at least one call outstanding. If two calls both run from second 0 to second 10, and each has a 10-second effective latency, summed tool time is 20 seconds while outstanding wall time is 10 seconds. Internal durations can also differ from those wall intervals, so overlap is not the only reason the measures can differ.

**Avg** is the average; P25/P50/P90/P99 are percentiles, with P50 the median. **N/A** means unavailable, not zero. No recorded tool calls means zero recorded tool time; zero or missing elapsed time has no percentage.

## Measurement reference

- **Population:** the descendant rows of the shared `artifacts/utils/subagent_cohorts.py::COHORT_SQL`, counted once per session. Resolved non-self roots must link to a positively identified main session; unknown-origin root anchors and unresolved links are excluded. Workflows are not filtered out. These are session-weighted distributions, not family averages.
- **Elapsed seconds:** latest minus earliest usable recorded timestamp, considering events and valid tool endpoints. No usable timestamps means elapsed time is unavailable.
- **Tool seconds:** the length of the union of valid emitted-call-to-result intervals. Missing or invalid timing for a recorded call makes this metric unavailable for that subagent, rather than treating the call as zero. A subagent with no recorded calls contributes zero, even if its elapsed span is unavailable.
- **Tool share:** `100 × tool seconds / elapsed seconds`, only when both times are available and elapsed time is positive. Avg is the average of per-subagent percentages, not a ratio of aggregate totals.
- **Summary:** finite eligible values use the shared NumPy linearly interpolated percentiles. Empty distributions are N/A. Counts appear once, with additional exclusions only for metrics whose eligible population differs. Truncated captures and missing events can bias all three distributions.

## Run and outputs

From the repository root:

```bash
uv run --no-sync python artifacts/subagents/timing/analyze.py --db trace/syfi_coding_trace.duckdb
uv run --no-sync --with pytest python -m pytest -q scripts/tests/test_subagent_timing.py
```

The shared CLI also accepts `--input path/to/normalized.jsonl` and `--output-dir path/to/output`; the source DB is read-only. Outputs are `timing.md` (three distribution tables with Claude/Codex rows and Avg/P25/P50/P90/P99 columns) and `headline.json` (a list of `{label, value}` objects showing each provider’s median observed elapsed seconds). No session identifiers or raw content are exported.
