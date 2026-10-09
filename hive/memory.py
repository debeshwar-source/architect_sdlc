"""Cross-run memory: recall of similar past runs.

Each finished run appends one record to memory/index.jsonl (outcome,
affected applications, the BLOCKING findings that came up, any human
decisions). On a new run, the requirement text is matched against past
records with BM25 and the best few are surfaced to the Requirement and
Architecture agents as reference.

The index is bounded (see condense()): durable lessons are pinned and
never condensed, one rolling summary stands in for old runs, and the
newest runs stay verbatim.

This is lexical recall (BM25), not embeddings -- deliberate, to keep
the package dependency-free. recall() is the only seam to swap if
embedding-based retrieval is wanted later.
"""

import json
import math
import os
import re
import shutil
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import List

import config

_lock = threading.RLock()  # reentrant: remember() calls condense()
_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "not", "are", "was", "has",
    "have", "from", "into", "should", "must", "will", "does", "but", "its",
    "when", "they", "their", "new", "use",
}
# A record must share at least this many distinct terms with the query
# to be recalled -- a score threshold alone is unreliable on a small
# corpus, where BM25's IDF collapses toward zero.
MIN_SHARED_TERMS = 2

# Bounded-memory shape. Condensing triggers once the index exceeds
# MAX_RECORDS; afterwards it holds <= pinned + 1 summary + KEEP_RECENT.
MAX_RECORDS = 200
KEEP_RECENT = 50
KEEP_BACKUPS = 3
SUMMARY_KIND = "summary"
K1, B = 1.5, 0.75


def _index_path():
    return config.MEMORY_DIR / "index.jsonl"


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2 and t not in _STOPWORDS]


def _load() -> List[dict]:
    path = _index_path()
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _doc_text(record: dict) -> str:
    return " ".join(
        [
            record.get("title", ""),
            record.get("requirement", ""),
            " ".join(record.get("affected_applications", [])),
            " ".join(record.get("blocking_findings", [])),
        ]
    )


def remember(state: dict) -> None:
    """Appends this run to memory. Skips runs that never produced a
    requirement (nothing worth recalling). Never raises."""
    try:
        requirement = state.get("requirement") or {}
        if not requirement:
            return
        findings = []
        for entry in state.get("history", []):
            if entry.get("stage") == "review":
                for f in (entry.get("result") or {}).get("findings", []):
                    if f.get("severity") == "BLOCKING":
                        findings.append(f.get("finding", ""))
        architecture = state.get("architecture") or {}
        record = {
            "run_id": state.get("run_id"),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": requirement.get("title", ""),
            "requirement": (state.get("requirement_text") or "").strip()[:600],
            "outcome": state.get("run_outcome"),
            "affected_applications": [a.get("name", "") for a in architecture.get("affected_applications", [])],
            "dependency_order": architecture.get("dependency_order", []),
            "blocking_findings": list(dict.fromkeys(findings))[:6],
            "breaker_level": (state.get("breaker") or {}).get("level", "ok"),
        }
        # Runs where the breaker acted are durable lessons -- pinned,
        # so condensing can never fold them away.
        record["pinned"] = record["breaker_level"] != "ok"
        with _lock:
            config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            with _index_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
            if len(_load()) > MAX_RECORDS:
                condense()
    except OSError:
        pass


def _summarize(old: List[dict]) -> dict:
    """Deterministic roll-up of runs being folded away (no LLM: cheap,
    reproducible, and cannot hallucinate a lesson). Earlier summaries
    in `old` are merged in, so the count and themes keep accumulating."""
    total = 0
    outcomes: Counter = Counter()
    apps: Counter = Counter()
    findings: Counter = Counter()
    for r in old:
        if r.get("kind") == SUMMARY_KIND:
            total += r.get("runs_summarized", 0)
            outcomes.update(r.get("outcome_counts", {}))
            apps.update(r.get("app_counts", {}))
            findings.update(r.get("finding_counts", {}))
            continue
        total += 1
        outcomes[r.get("outcome") or "UNKNOWN"] += 1
        apps.update(a for a in r.get("affected_applications", []) if a)
        findings.update(f for f in r.get("blocking_findings", []) if f)
    top_apps = [a for a, _ in apps.most_common(8)]
    top_findings = [f for f, _ in findings.most_common(8)]
    return {
        "kind": SUMMARY_KIND,
        "run_id": "condensed-history",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": f"Condensed history of {total} earlier runs",
        "requirement": "",
        "outcome": ", ".join(f"{n} {o}" for o, n in outcomes.most_common()),
        "affected_applications": top_apps,
        "blocking_findings": top_findings,
        "runs_summarized": total,
        "outcome_counts": dict(outcomes),
        "app_counts": dict(apps),
        "finding_counts": dict(findings),
        "pinned": False,
    }


def condense() -> bool:
    """Shrinks the index to: pinned records (untouched) + one rolling
    summary of everything older + the newest KEEP_RECENT records.

    Safe by layering, so a bug can never lose memory: backup first, build
    the new file beside the old one, verify it (counts reconcile, pinned
    records survive byte-for-byte, recent records are the newest), and
    only then atomically swap. Any failed check leaves the original
    untouched. Returns True if the index was rewritten."""
    path = _index_path()
    with _lock:
        records = _load()
        if len(records) <= KEEP_RECENT:
            return False

        pinned = [r for r in records if r.get("pinned")]
        unpinned = [r for r in records if not r.get("pinned")]
        recent = unpinned[-KEEP_RECENT:]
        old = unpinned[: len(unpinned) - len(recent)]
        if not old:
            return False

        summary = _summarize(old)
        new_records = pinned + [summary] + recent

        # Verify-don't-trust: totals must reconcile before anything is swapped.
        def runs_in(rs):
            return sum(r.get("runs_summarized", 1) if r.get("kind") == SUMMARY_KIND else 1 for r in rs)

        if runs_in(new_records) != runs_in(records):
            return False
        if [r for r in new_records if r.get("pinned")] != pinned:
            return False

        backup = path.with_name(f"{path.name}.bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}")
        shutil.copy2(path, backup)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                for r in new_records:
                    f.write(json.dumps(r, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            return False

        for stale in sorted(path.parent.glob(f"{path.name}.bak-*"))[:-KEEP_BACKUPS]:
            stale.unlink(missing_ok=True)
        return True


def recall(query: str, exclude_run_id: str = None, k: int = 2) -> List[dict]:
    records = [r for r in _load() if r.get("run_id") != exclude_run_id]
    if not records:
        return []
    docs = [Counter(_tokens(_doc_text(r))) for r in records]
    avg_len = (sum(sum(d.values()) for d in docs) / len(docs)) or 1.0
    df = Counter(t for d in docs for t in d)
    n = len(docs)

    scored = []
    for record, doc in zip(records, docs):
        length = sum(doc.values())
        score = 0.0
        shared = 0
        for term in set(_tokens(query)):
            if term not in doc:
                continue
            shared += 1
            idf = math.log(1 + n / df[term])
            tf = doc[term]
            score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / avg_len))
        if shared >= MIN_SHARED_TERMS:
            scored.append((score, record))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:k]]


def format_for_prompt(query: str, exclude_run_id: str = None) -> str:
    hits = recall(query, exclude_run_id)
    if not hits:
        return ""
    blocks = []
    for r in hits:
        if r.get("kind") == SUMMARY_KIND:
            blocks.append(
                f"- {r['title']} -- outcomes: {r.get('outcome')}\n"
                f"  most often affected: {', '.join(r.get('affected_applications') or []) or 'none'}\n"
                f"  review most often blocked on: {'; '.join(r.get('blocking_findings') or []) or 'n/a'}"
            )
            continue
        lines = [
            f"- {r.get('title') or r.get('run_id')} -- outcome: {r.get('outcome')}",
            f"  affected: {', '.join(r.get('affected_applications') or []) or 'none'}",
        ]
        if r.get("blocking_findings"):
            lines.append("  review blocked on: " + "; ".join(r["blocking_findings"]))
        blocks.append("\n".join(lines))
    body = "\n".join(blocks)
    return f"""
============================================================
SIMILAR PAST RUNS (memory -- reference only, NOT authoritative)
============================================================

Earlier runs on similar requirements. Use them to anticipate pitfalls
(especially what review blocked on), but decide from THIS requirement
and the knowledge base; do not copy their scope.

{body}
"""
