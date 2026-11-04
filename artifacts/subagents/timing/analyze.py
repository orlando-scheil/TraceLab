#!/usr/bin/env python3
"""Observed elapsed time and outstanding-tool wall time in matched subagent sessions."""

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


def union_duration(intervals) -> int:
    """Union valid intervals in integer microseconds, including touching intervals."""
    if not intervals:
        return 0
    ordered = sorted(intervals)
    start, end = ordered[0]
    total = 0
    for next_start, next_end in ordered[1:]:
        if next_start > end:
            total += end - start
            start = next_start
        end = max(end, next_end)
    return total + end - start


def collect(con) -> dict:
    sessions = {
        (provider, sid): {"bounds": [], "intervals": [], "complete": True}
        for provider, sid in con.execute(f"""
            WITH cohort AS ({COHORT_SQL})
            SELECT provider, session_id FROM cohort WHERE role = 'descendant'
        """).fetchall()
    }
    # Empty/unscoped/legacy cohorts need no timing columns beyond trace_db's compatibility views.
    if sessions:
        for provider, sid, first, last in con.execute(f"""
            WITH cohort AS ({COHORT_SQL})
            SELECT c.provider, c.session_id, min(epoch_us(t.timestamp)), max(epoch_us(t.timestamp))
            FROM cohort c
            JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
            JOIN timing_events t ON t.round_pk = r.round_pk
            WHERE c.role = 'descendant' AND isfinite(t.timestamp)
            GROUP BY c.provider, c.session_id
        """).fetchall():
            sessions[provider, sid]["bounds"] = [first, last]

        # Keep tools separate from timing events: joining both child tables would multiply calls.
        for provider, sid, start, end in con.execute(f"""
            WITH cohort AS ({COHORT_SQL})
            SELECT c.provider, c.session_id,
                   CASE WHEN isfinite(t.emitted_at) THEN epoch_us(t.emitted_at) END,
                   CASE WHEN isfinite(t.result_at) THEN epoch_us(t.result_at) END
            FROM cohort c
            JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
            JOIN tool_calls t ON t.round_pk = r.round_pk
            WHERE c.role = 'descendant'
        """).fetchall():
            session = sessions[provider, sid]
            if start is None or end is None or end < start:
                session["complete"] = False
            else:
                session["bounds"].extend((start, end))
                session["intervals"].append((start, end))

    out = {provider: {"sessions": 0, "elapsed": [], "tools": [], "share": []} for provider, _ in PROVIDERS}
    for (provider, _), session in sessions.items():
        bounds = session["bounds"]
        elapsed = (max(bounds) - min(bounds)) / 1_000_000 if bounds else None
        tools = union_duration(session["intervals"]) / 1_000_000 if session["complete"] else None
        share = tools / elapsed * 100 if tools is not None and elapsed is not None and elapsed > 0 else None
        out[provider]["sessions"] += 1
        out[provider]["elapsed"].append(elapsed)
        out[provider]["tools"].append(tools)
        out[provider]["share"].append(share)
    for data in out.values():
        for metric in ("elapsed", "tools", "share"):
            data[metric] = summarize(data[metric])
    return out


def _number(value) -> str:
    return "N/A" if value is None else f"{value:,.2f}"


def render_md(data: dict) -> str:
    blocks = ["# Observed subagent timing\n"]
    for metric, heading, description in (
        ("elapsed", "Observed elapsed time (seconds)",
         "Missing events can shorten the observed span."),
        ("tools", "Time with tool calls outstanding (seconds)",
         "Missing or invalid call timestamps make a session's tool time unavailable."),
        ("share", "Tool share of elapsed time (%)",
         "Percentage of each subagent's observed time with tools awaiting results. "
         "Avg averages these percentages, not provider totals. Zero or missing elapsed time has no percentage. "
         "This does not show whether the model was blocked or how long it spent generating."),
    ):
        blocks.append(f"## {heading}\n")
        if metric == "elapsed":
            blocks.append("Subagents: " + "; ".join(
                f"{label} {data[provider]['sessions']:,}" for provider, label in PROVIDERS
            ) + ". Includes workflow subagents and subagents launched by other subagents; excludes main sessions.\n")
        blocks.append(description + "\n")
        if any(data[provider][metric]["n"] != data[provider]["sessions"] for provider, _ in PROVIDERS):
            blocks.append("Eligible sessions: " + "; ".join(
                f"{label} {data[provider][metric]['n']:,} "
                f"({data[provider]['sessions'] - data[provider][metric]['n']:,} excluded)"
                for provider, label in PROVIDERS
            ) + ".\n")
        blocks.append(md_table.gfm_table(["Provider", "Avg", "P25", "P50", "P90", "P99"], [
            [label] + [_number(data[provider][metric][stat]) for stat in ("avg", "p25", "p50", "p90", "p99")]
            for provider, label in PROVIDERS
        ]))
    return "\n".join(blocks)


def render_headline(data: dict) -> list[dict[str, str]]:
    return [
        {"label": f"{label} median observed subagent elapsed time (seconds)",
         "value": _number(data[provider]["elapsed"]["p50"])}
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
    md_path = out_dir / "timing.md"
    md_path.write_text(render_md(data), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(data), indent=2) + "\n", encoding="utf-8")
    print(render_md(data))
    print(f"Wrote {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
