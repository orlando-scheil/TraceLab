#!/usr/bin/env python3
"""Compare main and descendant tool usage within linked Claude and Codex families."""

from __future__ import annotations

import argparse
import csv
import html
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import md_table  # noqa: E402
import png_sidecar  # noqa: E402
import trace_db  # noqa: E402
from subagent_cohorts import COHORT_SQL, summarize  # noqa: E402
from style import (  # noqa: E402
    BAR_BLUE, BAR_GREEN, PROVIDER_COMPARISON_ORDER, mticker, plt,
    polish_axes, provider_title, save_plot, short_label,
)

ROLES = ("main", "descendant")
TOP_TOOLS = 12
COUNTS = ("calls", "errors", "successes", "unknown_outcomes",
          "positive_latency_calls", "missing_latency_calls", "nonpositive_latency_calls")
STATS = ("n", "avg", "p25", "p50", "p90", "p99")
FIELDS = ["row_type", "provider", "role", "tool_name", *COUNTS,
          "error_rate", "known_outcome_coverage", "call_share", "latency_ms",
          "latency_share", *[f"diversity_{key}" for key in STATS]]


def load_metrics(con):
    """Aggregate calls once; separately left-join sessions to retain zero-tool diversity."""
    result = con.execute(f"""
        WITH cohort AS ({COHORT_SQL}), calls AS (
            SELECT c.provider, c.role,
                   CASE WHEN tc.tool_name IS NULL OR trim(tc.tool_name) = ''
                        THEN NULL ELSE tc.tool_name END AS tool_name,
                   tc.is_error, ({trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL}) AS eff
            FROM cohort c
            JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
            JOIN tool_calls tc ON tc.round_pk = r.round_pk
        )
        SELECT provider, role, tool_name, count(*) AS calls,
               count(*) FILTER (WHERE is_error = TRUE) AS errors,
               count(*) FILTER (WHERE is_error = FALSE) AS successes,
               count(*) FILTER (WHERE is_error IS NULL) AS unknown_outcomes,
               count(*) FILTER (WHERE eff > 0) AS positive_latency_calls,
               count(*) FILTER (WHERE eff IS NULL) AS missing_latency_calls,
               count(*) FILTER (WHERE eff <= 0) AS nonpositive_latency_calls,
               sum(eff) FILTER (WHERE eff > 0) AS latency_ms
        FROM calls GROUP BY provider, role, tool_name
        ORDER BY provider, role, calls DESC, tool_name NULLS LAST
    """)
    columns = [col[0] for col in result.description]
    tools = [dict(zip(columns, row)) for row in result.fetchall()]
    diversity = con.execute(f"""
        WITH cohort AS ({COHORT_SQL})
        SELECT c.provider, c.role, count(DISTINCT CASE
                   WHEN trim(tc.tool_name) <> '' THEN tc.tool_name END) AS named_tools
        FROM cohort c
        LEFT JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
        LEFT JOIN tool_calls tc ON tc.round_pk = r.round_pk
        GROUP BY c.provider, c.role, c.session_id
    """).fetchall()
    groups = []
    for provider in PROVIDER_COMPARISON_ORDER:
        for role in ROLES:
            rows = [r for r in tools if (r["provider"], r["role"]) == (provider, role)]
            stats = summarize([n for p, r, n in diversity if (p, r) == (provider, role)])
            group = {"row_type": "role", "provider": provider, "role": role,
                     **{f"diversity_{k}": v for k, v in stats.items()},
                     **{k: sum(r[k] for r in rows) for k in COUNTS}}
            group["latency_ms"] = (sum(r["latency_ms"] or 0 for r in rows)
                                   if group["positive_latency_calls"] else None)
            for row in [group, *rows]:
                known = row["errors"] + row["successes"]
                row["error_rate"] = row["errors"] / known if known else None
                row["known_outcome_coverage"] = known / row["calls"] if row["calls"] else None
                row["call_share"] = row["calls"] / group["calls"] if group["calls"] else None
                row["latency_share"] = ((row["latency_ms"] or 0) / group["latency_ms"]
                                        if group["latency_ms"] else None)
            groups.append(group)
    for row in tools:
        row["row_type"] = "tool"
    return groups, tools


def selected_names(tools):
    """One ranked union for every panel; exact names, no MCP aliases or role-local ranks."""
    counts = {}
    for row in tools:
        name = row["tool_name"]
        counts[name] = counts.get(name, 0) + row["calls"]
    return sorted(counts, key=lambda name: (-counts[name], name is None, name or ""))[:TOP_TOOLS]


def display_name(name):
    return "[unnamed calls]" if name is None else name


def plot_usage(groups, tools, output_dir):
    names = selected_names(tools)
    fig, axes = plt.subplots(2, 2, figsize=(16, max(9, len(names) * 0.8 + 3)), squeeze=False)
    try:
        fig.suptitle("Tool usage in linked subagent families", fontsize=18)
        lookup = {(r["provider"], r["role"], r["tool_name"]): r for r in tools}
        latency_max = max((r["latency_ms"] or 0 for r in tools), default=0) / 3_600_000
        for index, provider in enumerate(PROVIDER_COMPARISON_ORDER):
            provider_groups = [r for r in groups if r["provider"] == provider]
            for column, metric in enumerate(("call_share", "latency_ms")):
                ax = axes[index, column]
                title = "Call mix within each role" if column == 0 else "Additive positive tool time"
                ax.set_title(f"{provider_title(provider)} — {title}", loc="left", pad=12)
                if not any(r["diversity_n"] for r in provider_groups):
                    ax.text(0.5, 0.5, "No linked data", ha="center", va="center", transform=ax.transAxes)
                    ax.set_axis_off()
                    continue
                available = False
                notes = []
                for role_index, (role, color) in enumerate(zip(ROLES, (BAR_BLUE, BAR_GREEN))):
                    group = next(r for r in provider_groups if r["role"] == role)
                    valid = group["calls"] if column == 0 else group["positive_latency_calls"]
                    if not valid:
                        message = "no calls" if not group["calls"] else "no positive latency recorded"
                        notes.append(f"{role.title()}: {message}")
                        continue
                    available = True
                    positions, values = [], []
                    for i, name in enumerate(names):
                        row = lookup.get((provider, role, name))
                        # Missing latency is unavailable, not a measured zero-duration bar.
                        if column == 1 and (row is None or row[metric] is None):
                            continue
                        positions.append(i + (role_index - 0.5) * 0.36)
                        value = row[metric] if row else 0
                        values.append(value if column == 0 else value / 3_600_000)
                    ax.barh(positions, values, height=0.34, color=color, alpha=0.78,
                            label=f"{role.title()} (sessions: {group['diversity_n']:,})")
                if notes:
                    ax.set_title(f"{provider_title(provider)} — {title}\n" + "; ".join(notes),
                                 loc="left", pad=12)
                ax.set_yticks(range(len(names)), [short_label(display_name(n), 32) for n in names])
                ax.set_ylim(max(len(names), 3) + 0.5, -0.6)
                ax.set_xlabel("Share of all role calls" if column == 0 else "Summed effective latency (hours; not elapsed)")
                ax.set_xlim(0, 1 if column == 0 else (latency_max * 1.08 or 1))
                if column == 0:
                    ax.xaxis.set_major_formatter(mticker.PercentFormatter(1))
                polish_axes(ax, grid_axis="x")
                if available:
                    ax.legend(loc="lower right")
        omitted = sum(r["calls"] for r in tools if r["tool_name"] not in names)
        fig.text(0.5, 0.01,
                 f"Shared top {len(names)} names by combined calls; omitted tail: {omitted:,} calls. "
                 "All tool rows and coverage are in the tables/CSV.", ha="center", fontsize=10)
        fig.tight_layout(rect=(0, 0.04, 1, 0.96), h_pad=2, w_pad=3)
        save_plot(fig, output_dir / "tool_usage.png")
    finally:
        plt.close(fig)


def format_value(value):
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.4g}"
    return str(value)


def write_tables(groups, tools, output_dir):
    with (output_dir / "tool_usage.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows([*groups, *tools])
    blocks = ["# Tool usage in linked subagent families\n",
              "Claude and Codex are separate populations. Main sessions are counted once, "
              "not once per child. N/A means no eligible measurement; rates/shares are fractions.\n"]
    for provider in PROVIDER_COMPARISON_ORDER:
        if not any(r["diversity_n"] for r in groups if r["provider"] == provider):
            blocks.append(f"**{provider_title(provider)}: no linked data.**\n")
    summary_columns = ["provider", "role", "diversity_n", "calls", "errors", "successes",
                       "unknown_outcomes", "error_rate", "known_outcome_coverage"]
    blocks += ["## Role totals and outcome coverage\n", md_table.gfm_table(summary_columns,
               [[format_value(r[k]) for k in summary_columns] for r in groups])]
    diversity_columns = ["provider", "role", *[f"diversity_{k}" for k in STATS]]
    blocks += ["## Distinct named tools per session (including zero-call sessions)\n",
               md_table.gfm_table(diversity_columns,
               [[format_value(r[k]) for k in diversity_columns] for r in groups])]
    names = selected_names(tools)
    tail = []
    for group in groups:
        rows = [r for r in tools if (r["provider"], r["role"]) == (group["provider"], group["role"])
                and r["tool_name"] not in names]
        calls = sum(r["calls"] for r in rows)
        tail.append([group["provider"], group["role"], len(rows), calls,
                     format_value(calls / group["calls"] if group["calls"] else None)])
    blocks += [f"## Figure selection: shared top {len(names)} names; omitted tail\n",
               "Ranked by combined calls across providers and roles; ties by exact name. "
               "The same names and axes are used in both provider rows and both panels.\n",
               md_table.gfm_table(["Provider", "Role", "Omitted tool buckets", "Omitted calls", "Role call share"], tail)]
    columns = ["provider", "role", "tool_name", "calls", "call_share", "errors", "successes",
               "unknown_outcomes", "error_rate", "known_outcome_coverage"]
    latency_columns = ["provider", "role", "tool_name", "positive_latency_calls",
                       "missing_latency_calls", "nonpositive_latency_calls", "latency_ms", "latency_share"]
    for heading, keys in (("All tools: calls, mix, and same-tool outcomes", columns),
                          ("All tools: additive positive effective time (not elapsed)", latency_columns)):
        body = []
        for row in tools:
            body.append([html.escape(display_name(row[k])).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
                         if k == "tool_name" else format_value(row[k]) for k in keys])
        blocks += [f"## {heading}\n", md_table.gfm_table(keys, body)]
    blocks.append("Error rate = TRUE / (TRUE + FALSE); known-outcome coverage = (TRUE + FALSE) / calls. "
                  "Call and latency shares use all tools in the same provider/role, including unnamed calls. "
                  "Only positive effective latency is summed; internal latency takes precedence even when nonpositive. "
                  "No positive observations means N/A, not measured zero. See README for cohort and comparison limits.\n")
    (output_dir / "tool_usage.md").write_text("\n".join(blocks), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()
    con = trace_db.open_from_args(args)
    try:
        groups, tools = load_metrics(con)
    finally:
        con.close()
    write_tables(groups, tools, args.output_dir)
    plot_usage(groups, tools, args.output_dir)
    png_sidecar.make_self_contained(
        args.output_dir, code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md", png_names=["tool_usage.png"], data_glob="tool_usage.csv",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
