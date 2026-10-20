#!/usr/bin/env python3
"""Observed subagent adoption, family shape, and lineage coverage by provider."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import md_table  # noqa: E402
import trace_db  # noqa: E402
from subagent_cohorts import COHORT_SQL, summarize  # noqa: E402

PROVIDERS = (("claude", "Claude"), ("codex", "Codex"))


def collect(con) -> dict:
    """Count observed identities, then summarize only positively identified matched families."""
    count_keys = (
        "observed_sessions", "main_origins", "subagent_origins", "unknown_origins",
        "eligible_mains", "unavailable_lineage", "resolved_descendants",
    )
    out: dict = {provider: dict.fromkeys(count_keys, 0) for provider, _ in PROVIDERS}
    for provider, *counts in con.execute("""
        SELECT provider, count(*),
               count(*) FILTER (WHERE session_origin IN ('claude_top_level_layout', 'codex_top_level_source')),
               count(*) FILTER (WHERE session_origin IN ('claude_subagent_layout', 'codex_subagent_source')),
               count(*) FILTER (WHERE session_origin = 'unknown'),
               count(*) FILTER (WHERE session_origin IN ('claude_top_level_layout', 'codex_top_level_source')
                   AND root_status IN ('resolved_explicit', 'resolved_chain')
                   AND resolved_root_session_id = session_id),
               count(*) FILTER (WHERE root_status = 'unavailable'),
               count(*) FILTER (WHERE root_status IN ('resolved_explicit', 'resolved_chain')
                   AND resolved_root_session_id <> session_id)
        FROM sessions GROUP BY provider
    """).fetchall():
        if provider in out:
            out[provider].update(zip(count_keys, counts))

    families = {}
    depths = {provider: [] for provider, _ in PROVIDERS}
    for provider, root, parent_status, parent_role, depth in con.execute(f"""
        WITH cohort AS ({COHORT_SQL})
        SELECT c.provider, c.root_session_id, s.parent_status, p.role, s.depth
        FROM cohort c
        JOIN sessions s ON s.provider = c.provider AND s.session_id = c.session_id
        LEFT JOIN cohort p ON p.provider = c.provider
                          AND p.session_id = s.observed_parent_session_id
                          AND p.root_session_id = c.root_session_id
        WHERE c.role = 'descendant'
    """).fetchall():
        family = families.setdefault((provider, root), {"descendants": 0, "direct": 0, "nested": 0})
        family["descendants"] += 1
        if parent_status == "resolved":
            if parent_role == "main":
                family["direct"] += 1
            elif parent_role == "descendant":
                family["nested"] += 1
        if depth is not None:
            depths[provider].append(depth)

    for provider, _ in PROVIDERS:
        data = out[provider]
        members = [family for (prov, _), family in families.items() if prov == provider]
        data["delegating_mains"] = len(members)
        data["matched_descendants"] = sum(f["descendants"] for f in members)
        data["excluded_resolved_descendants"] = data["resolved_descendants"] - data["matched_descendants"]
        data["known_parent_edges"] = sum(f["direct"] + f["nested"] for f in members)
        data["nested_families"] = sum(f["nested"] > 0 for f in members)
        data["flat_families"] = sum(f["direct"] == f["descendants"] for f in members)
        data["unknown_families"] = len(members) - data["nested_families"] - data["flat_families"]
        data["descendants_per_main"] = summarize([f["descendants"] for f in members])
        data["direct_children_per_main"] = summarize([f["direct"] for f in members])
        data["depth"] = summarize(depths[provider])
    return out


def _number(value) -> str:
    return "N/A" if value is None else f"{value:,.2f}"


def _share(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.1f}%" if denominator else "N/A"


def render_md(data: dict) -> str:
    blocks = [
        "# Observed subagent adoption\n",
        "Observed capture, not complete real execution: missing descendants are not evidence of "
        "no delegation. Unknown-origin root anchors are excluded from matched families. "
        "Unavailable linkage is not zero adoption; empty statistics are N/A.\n",
    ]
    for provider, label in PROVIDERS:
        d = data[provider]
        blocks.append(f"## {label}\n")
        blocks.append(md_table.gfm_table(["Population / coverage", "Sessions"], [
            ["Observed valid session identities", d["observed_sessions"]],
            ["Known main origins (M0)", d["main_origins"]],
            ["Known subagent origins (C0)", d["subagent_origins"]],
            ["Unknown origins (U)", d["unknown_origins"]],
            ["Eligible resolved self-root mains (M)", d["eligible_mains"]],
            ["Mains with resolved descendants (F)", d["delegating_mains"]],
            ["Matched descendants (D)", d["matched_descendants"]],
            ["All resolved non-self descendants", d["resolved_descendants"]],
            ["Resolved descendants excluded from matched families", d["excluded_resolved_descendants"]],
            ["Lineage unavailable", d["unavailable_lineage"]],
        ]))
        ratios = [
            ("Observed adoption (F/M)", d["delegating_mains"], d["eligible_mains"]),
            ("Main linkage eligibility (M/M0)", d["eligible_mains"], d["main_origins"]),
            ("Classified subagent share (C0/(M0+C0))", d["subagent_origins"], d["main_origins"] + d["subagent_origins"]),
            ("Unknown-origin share (U/(M0+C0+U))", d["unknown_origins"], d["observed_sessions"]),
            ("Matched / all resolved descendants", d["matched_descendants"], d["resolved_descendants"]),
            ("Validated within-family parent edges / D", d["known_parent_edges"], d["matched_descendants"]),
            ("Known depth / D", d["depth"]["n"], d["matched_descendants"]),
            ("Nested / (nested + flat)", d["nested_families"], d["nested_families"] + d["flat_families"]),
            ("Known topology / F", d["nested_families"] + d["flat_families"], d["delegating_mains"]),
        ]
        blocks.append(md_table.gfm_table(["Rate", "Numerator", "Denominator", "Share"], [
            [name, num, den, _share(num, den)] for name, num, den in ratios
        ]))
        blocks.append(md_table.gfm_table(["Observed family topology", "Families"], [
            ["Nested: validated descendant-to-descendant edge", d["nested_families"]],
            ["Flat: every matched descendant resolved directly to root", d["flat_families"]],
            ["Unknown", d["unknown_families"]],
        ]))
        distributions = (
            ("descendants_per_main", "Observed descendants / F main"),
            ("direct_children_per_main", "Known direct children / F main (including zero known)"),
            ("depth", "Known descendant depth (root = 0)"),
        )
        blocks.append(md_table.gfm_table(["Distribution", "N", "Avg", "P25", "P50", "P90", "P99"], [
            [name, d[key]["n"]] + [_number(d[key][stat]) for stat in ("avg", "p25", "p50", "p90", "p99")]
            for key, name in distributions
        ]))
    blocks.append(
        "Direct-child counts include zero *known* children when edges are missing; they are not "
        "complete child counts. Depth excludes unknown values. Nestedness requires a resolved "
        "edge between two descendants of the same matched root; root membership alone does not "
        "establish an edge, depth, or flatness.\n"
    )
    return "\n".join(blocks)


def render_headline(data: dict) -> list[dict[str, str]]:
    return [
        {"label": f"{label} observed adoption (F/M)",
         "value": _share(data[provider]["delegating_mains"], data[provider]["eligible_mains"])}
        for provider, label in PROVIDERS
    ] + [
        {"label": f"{label} descendants / F main", "value": _number(data[provider]["descendants_per_main"]["avg"])}
        for provider, label in PROVIDERS
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args(argv)
    con = trace_db.open_from_args(args)
    try:
        data = collect(con)
    finally:
        con.close()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "adoption.md"
    md_path.write_text(render_md(data), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(data), indent=2) + "\n", encoding="utf-8")
    print(render_md(data))
    print(f"Wrote {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
