#!/usr/bin/env python3
"""Compare recorded family token usage and report-associated recipient transitions."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import md_table  # noqa: E402
import trace_db  # noqa: E402
from subagent_cohorts import COHORT_SQL, summarize  # noqa: E402
from timing import MODEL_OUTPUT_EVENT_TYPES  # noqa: E402
from token_policies import should_subtract_previous_output  # noqa: E402
from trace_privacy import REPORT_SOURCE_PROVIDERS, validate_report_fields  # noqa: E402

PROVIDERS = (("claude", "Claude"), ("codex", "Codex"))
SOURCES = {provider: source for source, provider in REPORT_SOURCE_PROVIDERS.items()}
EVIDENCE = {"claude": "Claude — candidate only", "codex": "Codex — supported wait receipt"}
ROLES = (("main", "Known delegating main"), ("intermediate", "Intermediate recipient"))
METRICS = (("input_tokens_total", "Input"), ("output_tokens", "Output"))
STAT_KEYS = ("n", "avg", "p25", "p50", "p90", "p99")
DELTA_LABEL = "Net parent-context change at report-return transitions"
RESIDUAL_LABEL = "Inferred context residual after previous-output deduction"


def nonnegative(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def query_rows(con, sql):
    result = con.execute(sql)
    names = [column[0] for column in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


def collect_usage(rounds):
    families, sessions = defaultdict(list), defaultdict(list)
    for row in rounds:
        families[row["root_session_id"]].append(row)
        sessions[row["session_id"]].append(row)
    result = {"families": len(families), "rounds": len(rounds), "totals": {}, "context": {}}
    for column, _ in METRICS:
        values = {role: [] for role in ("main", "descendant", "family")}
        positive = majority = 0
        for rows in families.values():
            if not all(nonnegative(row[column]) for row in rows):
                continue
            main = sum(row[column] for row in rows if row["role"] == "main")
            descendant = sum(row[column] for row in rows if row["role"] == "descendant")
            for role, value in (("main", main), ("descendant", descendant), ("family", main + descendant)):
                values[role].append(value)
            positive += main + descendant > 0
            majority += descendant > main
        result["totals"][column] = {
            "roles": {role: {**summarize(v), "total": sum(v) if v else None} for role, v in values.items()},
            "excluded_families": len(families) - len(values["family"]),
            "invalid_rounds": sum(not nonnegative(row[column]) for row in rounds),
            "positive_families": positive,
            "descendant_majority_families": majority,
            "descendant_majority_fraction": majority / positive if positive else None,
        }
    for role in ("main", "descendant"):
        groups = [rows for rows in sessions.values() if rows[0]["role"] == role]
        first, peak = [], []
        valid_rounds = incomplete = 0
        for rows in groups:
            inputs = [row["input_tokens_total"] for row in rows if nonnegative(row["input_tokens_total"])]
            valid_rounds += len(inputs)
            incomplete += len(inputs) < len(rows)
            # SQL orders by round_index NULLS LAST, then ingest_seq; do not skip a missing first value.
            if nonnegative(rows[0]["input_tokens_total"]):
                first.append(rows[0]["input_tokens_total"])
            if inputs:
                peak.append(max(inputs))
        result["context"][role] = {
            "sessions": len(groups), "rounds": sum(map(len, groups)), "valid_rounds": valid_rounds,
            "incomplete_sessions": incomplete, "first": summarize(first), "peak": summarize(peak),
        }
    return result


def transition_values(previous, current):
    """Return signed usage-derived values and an explicit reason for each unavailable metric."""
    model = current["model"]
    if not model or not model.strip() or model != previous["model"]:
        return None, None, "model_missing_or_changed"
    for row in (previous, current):
        i, p, a = (row[key] for key in ("input_tokens_total", "prefix_tokens", "newly_append_tokens"))
        if not all(nonnegative(value) for value in (i, p, a)) or i != p + a:
            return None, None, "invalid_input_usage"
    delta = current["input_tokens_total"] - previous["input_tokens_total"]
    if not should_subtract_previous_output(current, "claude-and-gpt55"):
        return delta, None, "residual_model_policy"
    if min(previous["input_tokens_total"], current["input_tokens_total"]) <= 0:
        return delta, None, "residual_nonpositive_input"
    output = previous["output_tokens"]
    if not nonnegative(output) or output == 0:
        return delta, None, "residual_missing_or_nonpositive_output"
    return delta, delta - output, None


def collect_reports(con, rounds):
    by_pk = {row["round_pk"]: row for row in rounds}
    by_index = defaultdict(list)
    for row in rounds:
        by_index[row["provider"], row["session_id"], row["round_index"]].append(row)
    results = defaultdict(list)
    for row in query_rows(con, """
        SELECT r.provider, r.session_id, t.round_pk, t.tool_call_id, t.is_error,
               CASE WHEN isfinite(t.timestamp) THEN CAST(epoch_us(t.timestamp) AS BIGINT) END AS result_us
        FROM timing_events t JOIN rounds r ON r.round_pk = t.round_pk
        WHERE t.event_type = 'tool_result' AND t.tool_call_id IS NOT NULL
    """):
        results[row["provider"], row["session_id"], row["tool_call_id"]].append(row)
    output_types = ", ".join(f"'{kind}'" for kind in sorted(MODEL_OUTPUT_EVENT_TYPES))
    outputs = {row["round_pk"]: row for row in query_rows(con, f"""
        SELECT round_pk, min(CAST(epoch_us(timestamp) AS BIGINT)) AS first_output_us,
               count(*) != count(CASE WHEN isfinite(timestamp) THEN timestamp END) AS missing_output_time
        FROM timing_events WHERE event_type IN ({output_types}) GROUP BY round_pk
    """)}
    reports = {
        provider: {role: {"coverage": Counter(), "delta": [], "residual": []}
                   for role in ("main", "intermediate", "unclassified")}
        for provider, _ in PROVIDERS
    }
    transitions = {}
    for tool in query_rows(con, f"""
        WITH cohort AS ({COHORT_SQL})
        SELECT r.provider, r.session_id, r.round_index, t.round_pk, t.tool_call_id,
               t.report_source, t.report_session_ids, t.is_error, c.role,
               CASE WHEN isfinite(t.emitted_at) THEN CAST(epoch_us(t.emitted_at) AS BIGINT) END AS emitted_us,
               count(*) OVER (PARTITION BY r.provider, r.session_id, t.tool_call_id) AS occurrences
        FROM tool_calls t JOIN rounds r ON r.round_pk = t.round_pk
        LEFT JOIN cohort c ON c.provider = r.provider AND c.session_id = r.session_id
        WHERE r.provider IN ('claude', 'codex')
    """):
        provider, sid, call_id = (tool[key] for key in ("provider", "session_id", "tool_call_id"))
        role = {"main": "main", "descendant": "intermediate"}.get(tool["role"], "unclassified")
        coverage = reports[provider][role]["coverage"]
        coverage["tool_calls"] += 1
        if tool["report_source"] is None and tool["report_session_ids"] is None:
            coverage["no_report_evidence"] += 1
            continue
        coverage["annotated_entries"] += 1
        try:
            validate_report_fields(tool, provider)
        except ValueError:
            coverage["invalid_report_metadata"] += 1
            continue
        ids = tool["report_session_ids"]
        if ids is not None and sid in ids:
            coverage["invalid_report_metadata"] += 1
            continue
        coverage["evidence_entries"] += 1
        if tool["is_error"] is True:
            coverage["known_error_returns"] += 1
            continue
        if not call_id or not call_id.strip():
            coverage["missing_call_id"] += 1
            continue
        if tool["occurrences"] != 1:
            coverage["ambiguous_call_id"] += 1
            continue
        consumers = results[provider, sid, call_id]
        consumer_pks = {row["round_pk"] for row in consumers}
        if not consumer_pks:
            coverage["no_consuming_round"] += 1
            continue
        if len(consumer_pks) != 1:
            coverage["ambiguous_result_consumer"] += 1
            continue
        if any(row["is_error"] is True for row in consumers):
            coverage["known_error_returns"] += 1
            continue
        if role == "unclassified":
            coverage["recipient_not_in_cohort"] += 1
            continue
        current = by_pk[next(iter(consumer_pks))]
        if (tool["round_index"] is None or current["round_index"] is None
                or current["round_pk"] == tool["round_pk"]
                or current["round_index"] <= tool["round_index"]):
            coverage["not_later_round"] += 1
            continue
        output = outputs.get(current["round_pk"])
        arrivals = {row["result_us"] for row in consumers}
        if (tool["emitted_us"] is None or None in arrivals or output is None
                or output["missing_output_time"]):
            coverage["unavailable_consumption_timing"] += 1
            continue
        first_output = output["first_output_us"]
        if (len(arrivals) != 1 or min(arrivals) < tool["emitted_us"]
                or first_output < tool["emitted_us"]):
            coverage["impossible_consumption_timing"] += 1
            continue
        # Codex stores pending inputs before segment events, not in original arrival order.
        if next(iter(arrivals)) > first_output:
            coverage["report_after_model_output"] += 1
            continue
        coverage["mapped_entries"] += 1
        transitions[provider, sid, current["round_pk"]] = (role, current)
    for (provider, sid, _), (role, current) in transitions.items():
        block = reports[provider][role]
        coverage = block["coverage"]
        coverage["consuming_transitions"] += 1
        index = current["round_index"]
        previous = by_index[provider, sid, index - 1]
        if len(by_index[provider, sid, index]) != 1 or len(previous) != 1:
            coverage["nonconsecutive_or_ambiguous_rounds"] += 1
            continue
        delta, residual, reason = transition_values(previous[0], current)
        if delta is not None:
            block["delta"].append(delta)
        if residual is not None:
            block["residual"].append(residual)
        if reason:
            coverage[reason] += 1
    for groups in reports.values():
        for block in groups.values():
            block["delta"] = summarize(block["delta"])
            block["residual"] = summarize(block["residual"])
    return reports


def collect(con):
    rounds = query_rows(con, f"""
        WITH cohort AS ({COHORT_SQL})
        SELECT r.round_pk, r.ingest_seq, r.provider, r.session_id, r.round_index, r.model,
               r.input_tokens_total, r.prefix_tokens, r.newly_append_tokens, r.output_tokens,
               c.root_session_id, c.role
        FROM rounds r JOIN cohort c ON c.provider = r.provider AND c.session_id = r.session_id
        ORDER BY r.provider, r.session_id, r.round_index NULLS LAST, r.ingest_seq NULLS LAST
    """)
    return {
        "usage": {provider: collect_usage([row for row in rounds if row["provider"] == provider])
                  for provider, _ in PROVIDERS},
        "reports": collect_reports(con, rounds),
    }


def fmt(value):
    return "N/A" if value is None else f"{value:,.1f}".removesuffix(".0")


def fraction(value):
    return "N/A" if value is None else f"{100 * value:.1f}%"


def render_md(stats):
    sections = []
    for column, label in METRICS:
        rows = []
        for provider, name in PROVIDERS:
            metric = stats["usage"][provider]["totals"][column]
            for role in ("main", "descendant", "family"):
                s = metric["roles"][role]
                rows.append([name, role, fmt(s["total"])] + [fmt(s[key]) for key in STAT_KEYS])
        sections.append((f"Recorded {label.lower()} totals per complete family", rows))
    parts = [
        "# Tokens and reports\n\n"
        "Recorded usage only; no tokenizer or text-length estimate. Input totals sum repeated context "
        "across rounds, not unique tokens. Output includes reasoning once. Missing statistics are N/A.\n",
        md_table.section_tables(["Provider", "Role", "Pooled tokens", "Families", "Avg", "P25", "P50", "P90", "P99"], sections),
    ]
    coverage = []
    for column, label in METRICS:
        for provider, name in PROVIDERS:
            usage = stats["usage"][provider]
            s = usage["totals"][column]
            coverage.append([name, label, fmt(usage["families"]), fmt(usage["rounds"]),
                             fmt(s["excluded_families"]), fmt(s["invalid_rounds"]),
                             fmt(s["positive_families"]), fmt(s["descendant_majority_families"]),
                             fraction(s["descendant_majority_fraction"])])
    parts += ["## Family coverage and descendant-majority fraction\n",
              md_table.gfm_table(["Provider", "Metric", "Retained families", "Retained rounds", "Excluded families",
                                  "Invalid/missing rounds", "Eligible positive families", "Descendant-majority families", "Fraction"], coverage)]
    rows, coverage = [], []
    for provider, name in PROVIDERS:
        for role in ("main", "descendant"):
            s = stats["usage"][provider]["context"][role]
            for metric in ("first", "peak"):
                rows.append([name, role, metric] + [fmt(s[metric][key]) for key in STAT_KEYS])
            coverage.append([name, role, fmt(s["sessions"]), fmt(s["sessions"] - s["first"]["n"]),
                             fmt(s["sessions"] - s["peak"]["n"]), fmt(s["incomplete_sessions"]),
                             fmt(s["valid_rounds"]), fmt(s["rounds"])])
    parts += ["## First and peak observed input per session\n",
              md_table.gfm_table(["Provider", "Role", "Metric", "Sessions", "Avg", "P25", "P50", "P90", "P99"], rows),
              md_table.gfm_table(["Provider", "Role", "Sessions", "First unavailable", "Peak unavailable",
                                  "Incomplete input coverage", "Valid input rounds", "Retained rounds"], coverage),
              "First means the first retained round ordered by round_index then ingest_seq (nulls last), "
              "not the first valid value. Peak uses available nonnegative inputs; incomplete sessions can understate it.\n",
              "## Report-associated recipient transitions\n\n"
              "**Exact report-only token counts are not identifiable from retained usage.** These metrics are "
              "derived from recorded usage, not report-body token counts. Codex completed wait receipts are "
              "supported evidence; Claude Agent/Task results are candidates only, not confirmed completed reports. "
              "No report evidence means unavailable evidence, not observed zero returns.\n\n"
              f"**{DELTA_LABEL}** = I_current − I_previous. "
              f"**{RESIDUAL_LABEL}** = delta − O_previous, only for Claude and exact Codex `gpt-5.5`. "
              "Both are signed. Other messages/results, context removal, and compaction can contribute; "
              "neither statistic isolates report content. Each consuming transition is counted once.\n"]
    for role, role_label in ROLES:
        rows = []
        for provider, _ in PROVIDERS:
            block = stats["reports"][provider][role]
            for metric, label in (("delta", DELTA_LABEL), ("residual", RESIDUAL_LABEL)):
                rows.append([EVIDENCE[provider], label] + [fmt(block[metric][key]) for key in STAT_KEYS])
        parts += [f"### {role_label}\n", md_table.gfm_table(
            ["Evidence", "Metric", "Transitions", "Avg", "P25", "P50", "P90", "P99"], rows)]
    coverage_labels = (
        ("tool_calls", "Tool calls inspected"), ("no_report_evidence", "Calls without report evidence"),
        ("annotated_entries", "Annotated entries"), ("evidence_entries", "Supported/candidate entries"),
        ("invalid_report_metadata", "Invalid report metadata (excluded)"),
        ("known_error_returns", "Known errors (excluded)"), ("missing_call_id", "Missing call ID"),
        ("ambiguous_call_id", "Ambiguous call occurrence"), ("no_consuming_round", "No consuming round"),
        ("ambiguous_result_consumer", "Ambiguous result consumer"),
        ("recipient_not_in_cohort", "Recipient outside matched cohort"), ("not_later_round", "Not a later indexed round"),
        ("unavailable_consumption_timing", "Unavailable consumption timing"),
        ("impossible_consumption_timing", "Impossible/conflicting consumption timing"),
        ("report_after_model_output", "Report arrived after first model output"),
        ("mapped_entries", "Entries with valid consumption timing"), ("consuming_transitions", "Unique consuming transitions"),
        ("nonconsecutive_or_ambiguous_rounds", "Missing/duplicate consecutive round pair"),
        ("model_missing_or_changed", "Missing/changed model"), ("invalid_input_usage", "Invalid/inconsistent input usage"),
        ("residual_model_policy", "Residual: excluded model policy"),
        ("residual_nonpositive_input", "Residual: nonpositive input"),
        ("residual_missing_or_nonpositive_output", "Residual: missing/nonpositive previous output"),
    )
    for role, label in (*ROLES, ("unclassified", "Unclassified recipient (coverage only)")):
        blocks = [stats["reports"][p][role] for p, _ in PROVIDERS]
        rows = [["Evidence status"] + ["Evidence available" if b["coverage"]["evidence_entries"] else "No report evidence" for b in blocks]]
        rows += [[label] + [fmt(b["coverage"][key]) for b in blocks] for key, label in coverage_labels]
        rows += [["Eligible delta transitions"] + [fmt(b["delta"]["n"]) for b in blocks],
                 ["Eligible residual transitions"] + [fmt(b["residual"]["n"]) for b in blocks]]
        parts += [f"### Coverage — {label}\n", md_table.gfm_table(["Count / status", EVIDENCE["claude"], EVIDENCE["codex"]], rows)]
    parts.append("The recipient is the session owning the call and consuming its result, never its ultimate root. "
                 "Intermediate receipts are not main receipts. Entry exclusions use the first failing check; "
                 "consumption requires timed report arrival at or before the first model output, never event-list order. "
                 "transition exclusions apply after receipt deduplication. Null annotations, including legacy data, "
                 "cannot establish absence of returns. See README for eligibility and recording limits.\n")
    return "\n".join(parts)


def render_headline(stats):
    return [
        {"label": f"{name} median family {label.lower()}",
         "value": fmt(stats["usage"][provider]["totals"][column]["roles"]["family"]["p50"])}
        for provider, name in PROVIDERS for column, label in METRICS
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()
    con = trace_db.open_from_args(args)
    try:
        stats = collect(con)
    finally:
        con.close()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tokens_and_reports.md").write_text(render_md(stats), encoding="utf-8")
    (out / "headline.json").write_text(json.dumps(render_headline(stats), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out / 'tokens_and_reports.md'} and {out / 'headline.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
