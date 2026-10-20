#!/usr/bin/env python3
"""Retained rounds and tool calls in matched main/subagent sessions, by provider."""

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
ROLES = (("main", "Delegating mains (F), once each"), ("descendant", "Linked descendants (D)"))


def collect(con) -> dict:
    """Use one row per retained round; aggregate tools before joining to avoid fanout."""
    out = {
        provider: {"observed_sessions": 0, "unavailable_lineage": 0, "resolved_descendants": 0,
                   **{role: {"per_round": []} for role, _ in ROLES}}
        for provider, _ in PROVIDERS
    }
    for provider, observed, unavailable, resolved in con.execute("""
        SELECT provider, count(*), count(*) FILTER (WHERE root_status = 'unavailable'),
               count(*) FILTER (WHERE root_status IN ('resolved_explicit', 'resolved_chain')
                   AND resolved_root_session_id <> session_id)
        FROM sessions GROUP BY provider
    """).fetchall():
        if provider in out:
            out[provider].update(observed_sessions=observed, unavailable_lineage=unavailable,
                                 resolved_descendants=resolved)

    sessions = {}
    for provider, role, sid, tools in con.execute(f"""
        WITH cohort AS ({COHORT_SQL}),
        tools AS (SELECT round_pk, count(*) AS n FROM tool_calls GROUP BY round_pk)
        SELECT c.provider, c.role, c.session_id, coalesce(t.n, 0)
        FROM cohort c
        JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
        LEFT JOIN tools t ON t.round_pk = r.round_pk
    """).fetchall():
        counts = sessions.setdefault((provider, role, sid), [0, 0])
        counts[0] += 1
        counts[1] += tools
        out[provider][role]["per_round"].append(tools)

    for provider, _ in PROVIDERS:
        for role, _ in ROLES:
            data = out[provider][role]
            members = [counts for (prov, kind, _), counts in sessions.items() if (prov, kind) == (provider, role)]
            data["sessions"] = len(members)
            data["rounds"] = sum(rounds for rounds, _ in members)
            data["tools"] = sum(tools for _, tools in members)
            data["zero_tool_sessions"] = sum(tools == 0 for _, tools in members)
            data["one_round_sessions"] = sum(rounds == 1 for rounds, _ in members)
            data["rounds_per_session"] = summarize([rounds for rounds, _ in members])
            data["tools_per_session"] = summarize([tools for _, tools in members])
            data["tools_per_round"] = summarize(data.pop("per_round"))
        out[provider]["excluded_resolved_descendants"] = (
            out[provider]["resolved_descendants"] - out[provider]["descendant"]["sessions"]
        )
    return out


def _number(value) -> str:
    return "N/A" if value is None else f"{value:,.2f}"


def _share(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.1f}%" if denominator else "N/A"


def render_md(data: dict) -> str:
    blocks = [
        "# Observed subagent workload\n",
        "Retained capture, not complete real execution. Compare each delegating main once with "
        "its linked descendants; unrelated mains and unknown-origin root-anchor families are "
        "excluded. A round is a retained LLM row, not a request.\n",
    ]
    for provider, label in PROVIDERS:
        d = data[provider]
        blocks.append(f"## {label}\n")
        blocks.append(md_table.gfm_table(["Population / coverage", "Sessions"], [
            ["Observed valid session identities", d["observed_sessions"]],
            ["Matched delegating mains (F)", d["main"]["sessions"]],
            ["Matched descendants (D)", d["descendant"]["sessions"]],
            ["Outside matched cohorts", d["observed_sessions"] - d["main"]["sessions"] - d["descendant"]["sessions"]],
            ["All resolved non-self descendants", d["resolved_descendants"]],
            ["Resolved descendants excluded from matched families", d["excluded_resolved_descendants"]],
            ["Lineage unavailable", d["unavailable_lineage"]],
        ]))
        for role, role_label in ROLES:
            s = d[role]
            blocks.append(f"### {role_label}\n")
            blocks.append(md_table.gfm_table(["Retained population", "Count"], [
                ["Sessions", s["sessions"]], ["Rounds", s["rounds"]], ["Tool calls", s["tools"]],
            ]))
            blocks.append(md_table.gfm_table(["Distribution", "N", "Avg", "P25", "P50", "P90", "P99"], [
                [name, s[key]["n"]] + [_number(s[key][stat]) for stat in ("avg", "p25", "p50", "p90", "p99")]
                for key, name in (("rounds_per_session", "Retained rounds / session"),
                                  ("tools_per_session", "Tool calls / session (including zero)"),
                                  ("tools_per_round", "Tool calls / round (including zero)"))
            ]))
            blocks.append(md_table.gfm_table(["Observed share", "Sessions", "Denominator", "Share"], [
                [name, s[key], s["sessions"], _share(s[key], s["sessions"])]
                for key, name in (("zero_tool_sessions", "Zero-tool sessions"),
                                  ("one_round_sessions", "One-observed-round sessions (not success)"))
            ]))
    blocks.append(
        "Tools are counted separately by round_pk before joining rounds; duplicate natural round "
        "IDs remain separate retained observations. Per-round means equal total tools / total "
        "retained rounds, not an average of session means. Zero tool rows means zero observed "
        "calls, not proof that no tools ran outside capture. One observed round is not success, "
        "completion, or a single user request. Empty populations are N/A, never zero workload.\n"
    )
    return "\n".join(blocks)


def render_headline(data: dict) -> list[dict[str, str]]:
    return [
        {"label": f"{label} {role} rounds / session", "value": _number(data[provider][role]["rounds_per_session"]["avg"])}
        for provider, label in PROVIDERS for role, _ in ROLES
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
    md_path = out_dir / "workload.md"
    md_path.write_text(render_md(data), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(data), indent=2) + "\n", encoding="utf-8")
    print(render_md(data))
    print(f"Wrote {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
