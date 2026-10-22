"""Reads runs/token_log.jsonl and prints, per agent, where the tokens go.

    python3 token_report.py [--since 2026-10-15T06:00] [--agent Design]

Plain words: for each agent it shows how many calls were made, how big the answer really was
versus how many output tokens were billed (the difference is hidden reasoning), how much input came
from the cache, how many turns the CLI took, what it cost, and why answers were rejected.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import config


def load(since: str = "", agent: str = "", path: Optional[Path] = None) -> List[dict]:
    path = path or config.RUNS_ROOT / "token_log.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if since and r.get("ts", "") < since:
            continue
        if agent and agent.lower() not in r.get("agent", "").lower():
            continue
        rows.append(r)
    return rows


def _key(agent: str) -> str:
    return (agent or "?").split(" ")[0].lower()


def summarise(rows: List[dict]) -> Dict[str, dict]:
    """Per agent: averages over its finished calls, plus counts of attempts and rejections, and how often
    it read beyond its one-pager, had a skill file injected, or had to be asked again for lack of a read."""
    acc: Dict[str, dict] = defaultdict(lambda: defaultdict(float))
    rejections: Dict[str, List[str]] = defaultdict(list)
    ctx: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        k = _key(r.get("agent", "?"))
        if r["event"] == "context_read":
            ctx[k]["reads"] += 1
        elif r["event"] == "skill_inject":
            ctx[k]["skill_injections"] += 1
        elif r["event"] == "kb_fallback":
            ctx[k]["reasks"] += 1
            ctx[k]["reasks_with_no_reads"] += int(r.get("reads", 1) == 0)
    for r in rows:
        a = r.get("agent", "?")
        if r["event"] == "llm_attempt" and not r.get("ok"):
            acc[a]["failed_attempts"] += 1
            rejections[a].append(str(r.get("error", ""))[:120])
        if r["event"] != "llm_call":
            continue
        u, st = r.get("usage") or {}, r.get("stats") or {}
        d = acc[a]
        d["calls"] += 1
        d["ok"] += bool(r.get("ok"))
        d["attempts"] += r.get("attempts", 1)
        d["fresh_in"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        d["cache_read"] += u.get("cache_read_input_tokens", 0)
        d["out"] += u.get("output_tokens", 0)
        if "answer_tokens_est" in r:                 # newer rows only: older ones did not record these
            d["answer_calls"] += 1
            d["answer"] += r["answer_tokens_est"]
            d["out_when_answer"] += u.get("output_tokens", 0)
        if st:
            d["stat_calls"] += 1
            d["thinking"] += st.get("thinking_tokens_est", 0)
            d["turns"] += st.get("turns", 0)
            d["cost"] += st.get("cost_usd", 0)
    result = {}
    for a, d in acc.items():
        n = max(d["calls"], 1)
        total_in = d["fresh_in"] + d["cache_read"]
        result[a] = {
            "calls": int(d["calls"]), "ok": int(d["ok"]), "attempts_avg": round(d["attempts"] / n, 2),
            "fresh_input_avg": round(d["fresh_in"] / n), "cache_read_avg": round(d["cache_read"] / n),
            "cache_hit_rate": round(d["cache_read"] / total_in, 2) if total_in else 0.0,
            "output_billed_avg": round(d["out"] / n),
            "answer_avg": round(d["answer"] / max(d["answer_calls"], 1)),
            "hidden_output_share": round(1 - d["answer"] / d["out_when_answer"], 2) if d["out_when_answer"] else None,
            "thinking_est_avg": round(d["thinking"] / max(d["stat_calls"], 1)),
            "turns_avg": round(d["turns"] / max(d["stat_calls"], 1), 1) if d["stat_calls"] else None,
            "cost_usd_avg": round(d["cost"] / max(d["stat_calls"], 1), 3) if d["stat_calls"] else None,
            "failed_attempts": int(d["failed_attempts"]),
            "reads_avg": round(ctx[_key(a)]["reads"] / n, 1), "skill_injections": ctx[_key(a)]["skill_injections"],
            "reasks": ctx[_key(a)]["reasks"], "reasks_with_no_reads": ctx[_key(a)]["reasks_with_no_reads"],
            "rejection_reasons": rejections.get(a, [])[:5],
        }
    return result


def render(summary: Dict[str, dict]) -> str:
    if not summary:
        return "No finished calls in the ledger yet."
    lines = []
    for agent, s in sorted(summary.items()):
        lines.append(f"{agent}: {s['calls']} call(s), {s['ok']} ok, {s['attempts_avg']} attempt(s) each")
        lines.append(f"   input   fresh {s['fresh_input_avg']:,}   from cache {s['cache_read_avg']:,}   (cache hit {s['cache_hit_rate']:.0%})")
        hidden = "n/a" if s["hidden_output_share"] is None else f"{s['hidden_output_share']:.0%}"
        lines.append(f"   output  billed {s['output_billed_avg']:,}   answer {s['answer_avg']:,}   hidden {hidden}"
                     f"   (thinking seen ~{s['thinking_est_avg']:,})")
        lines.append(f"   turns {s['turns_avg'] if s['turns_avg'] is not None else 'n/a'}   "
                     f"cost {'n/a' if s['cost_usd_avg'] is None else '$' + str(s['cost_usd_avg'])}   failed attempts {s['failed_attempts']}")
        lines.append(f"   reads beyond the one-pager {s['reads_avg']} per call   skill files injected {s['skill_injections']}   "
                     f"asked again for missing reads {s['reasks']} (of which with no reads at all {s['reasks_with_no_reads']})")
        for why in s["rejection_reasons"]:
            lines.append(f"     rejected: {why}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="")
    ap.add_argument("--agent", default="")
    args = ap.parse_args()
    print(render(summarise(load(args.since, args.agent))))
