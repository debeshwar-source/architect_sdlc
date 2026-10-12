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

Plain words: the system's long-term memory. After each run it jots down a
short summary (what was asked, what was designed, what risks were flagged). When a new request arrives, it searches those summaries for similar
past requests and shows the best matches to the AI as "lessons learned".
Matching is done by shared keywords (ranked with a standard formula
called BM25), not by anything fancy. To stop the file growing forever,
old entries are periodically rolled into one summary entry.
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
from hive import blackboard

_lock = threading.RLock()  # reentrant: remember() calls condense()
# Filler words ignored when matching, so only meaningful words count.
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
MAX_RECORDS = 200      # once memory holds more than this many entries, shrink it
KEEP_RECENT = 50       # when shrinking, keep this many newest entries untouched
KEEP_BACKUPS = 3       # how many safety copies of the memory file to retain
SUMMARY_KIND = "summary"  # label marking the rolled-up "old history" entry
# Tuning numbers for the BM25 ranking formula (standard textbook values).
K1, B = 1.5, 0.75


def _index_path():
    # The memory file: one JSON record per line.
    return config.MEMORY_DIR / "index.jsonl"


def _tokens(text: str) -> List[str]:
    # Split text into lowercase keywords (letters/digits, longer than 2
    # characters, not filler words).
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2 and t not in _STOPWORDS]


def _load() -> List[dict]:
    # Read every saved record from the memory file (none if it doesn't exist
    # yet). Damaged lines are skipped.
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


def _risks(record: dict) -> List[str]:
    # The risks recorded for a run. Older records used the key
    # "blocking_findings" for this, so read that too.
    return record.get("risks") or record.get("blocking_findings") or []


def _doc_text(record: dict) -> str:
    # The searchable text of one record: title, original request, affected
    # apps and the risks the design flagged, joined together.
    return " ".join(
        [
            record.get("title", ""),
            record.get("requirement", ""),
            " ".join(record.get("affected_applications", [])),
            " ".join(_risks(record)),
        ]
    )


def remember(state: dict) -> None:
    """Appends this run to memory. Skips runs that never produced a
    requirement (nothing worth recalling). Never raises."""
    try:
        requirement = state.get("requirement") or {}
        # No requirement was produced -> nothing useful to remember.
        if not requirement:
            return
        # The risks the design flagged (kept as plain text).
        design = state.get("design") or {}
        risks = [r if isinstance(r, str) else json.dumps(r, default=str) for r in design.get("architecture_risks", [])]
        # Every application involved: existing ones changed and new ones built.
        involved = [a.get("name", "") for a in design.get("affected_applications", [])] + [
            a.get("name", "") for a in design.get("new_applications_needed", [])
        ]
        # The compact summary of this run that will be stored.
        record = {
            "run_id": state.get("run_id"),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": requirement.get("title", ""),
            "requirement": (state.get("requirement_text") or "").strip()[:600],
            "outcome": state.get("run_outcome"),
            "affected_applications": involved,
            "dependency_order": design.get("dependency_order", []),
            "risks": list(dict.fromkeys(risks))[:6],
        }
        # Runs where a human steered the outcome are durable lessons --
        # pinned, so condensing can never fold them away.
        record["pinned"] = bool(blackboard.read(state.get("run_id") or "")["human_decisions"])
        with _lock:
            config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            # Add the record as a new last line of the memory file.
            with _index_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
            # Too many records? Shrink the file.
            if len(_load()) > MAX_RECORDS:
                condense()
    except OSError:
        # Memory is a nice-to-have; a disk problem must not break the run.
        pass


def _summarize(old: List[dict]) -> dict:
    """Deterministic roll-up of runs being folded away (no LLM: cheap,
    reproducible, and cannot hallucinate a lesson). Earlier summaries
    in `old` are merged in, so the count and themes keep accumulating."""
    # Counters tally how often each item appears (like tally marks).
    total = 0
    outcomes: Counter = Counter()
    apps: Counter = Counter()
    findings: Counter = Counter()
    for r in old:
        # An older roll-up: carry its totals forward.
        if r.get("kind") == SUMMARY_KIND:
            total += r.get("runs_summarized", 0)
            outcomes.update(r.get("outcome_counts", {}))
            apps.update(r.get("app_counts", {}))
            findings.update(r.get("finding_counts", {}))
            continue
        # An ordinary run: count it and tally its outcome, apps and findings.
        total += 1
        outcomes[r.get("outcome") or "UNKNOWN"] += 1
        apps.update(a for a in r.get("affected_applications", []) if a)
        findings.update(f for f in _risks(r) if f)
    # Keep only the 8 most frequent applications and complaints.
    top_apps = [a for a, _ in apps.most_common(8)]
    top_findings = [f for f, _ in findings.most_common(8)]
    # The single roll-up record that replaces all the old ones.
    return {
        "kind": SUMMARY_KIND,
        "run_id": "condensed-history",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": f"Condensed history of {total} earlier runs",
        "requirement": "",
        "outcome": ", ".join(f"{n} {o}" for o, n in outcomes.most_common()),
        "affected_applications": top_apps,
        "risks": top_findings,
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
        # Already small enough -> nothing to do.
        if len(records) <= KEEP_RECENT:
            return False

        # Split into: "pinned" (important lessons, never folded away),
        # the newest few (kept as-is), and everything older (rolled up).
        pinned = [r for r in records if r.get("pinned")]
        unpinned = [r for r in records if not r.get("pinned")]
        recent = unpinned[-KEEP_RECENT:]
        old = unpinned[: len(unpinned) - len(recent)]
        if not old:
            return False

        summary = _summarize(old)
        new_records = pinned + [summary] + recent

        # Safety check 1: the total number of runs represented must be
        # unchanged.
        # Verify-don't-trust: totals must reconcile before anything is swapped.
        def runs_in(rs):
            return sum(r.get("runs_summarized", 1) if r.get("kind") == SUMMARY_KIND else 1 for r in rs)

        if runs_in(new_records) != runs_in(records):
            return False
        # Safety check 2: pinned records must be exactly as before.
        if [r for r in new_records if r.get("pinned")] != pinned:
            return False

        # Make a backup copy, write the new version to a temporary file,
        # then swap it into place in one step - so a crash can never leave
        # a half-written memory file.
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

        # Delete the oldest backups, keeping only the last few.
        for stale in sorted(path.parent.glob(f"{path.name}.bak-*"))[:-KEEP_BACKUPS]:
            stale.unlink(missing_ok=True)
        return True


def recall(query: str, exclude_run_id: str = None, k: int = 2) -> List[dict]:
    """Find the `k` past runs most similar to `query` (the new request)."""
    # Consider every past run except the current one.
    records = [r for r in _load() if r.get("run_id") != exclude_run_id]
    if not records:
        return []
    # Turn each record into a keyword tally, plus some averages that the
    # BM25 formula needs: average length, and in how many records each
    # word appears (df).
    docs = [Counter(_tokens(_doc_text(r))) for r in records]
    avg_len = (sum(sum(d.values()) for d in docs) / len(docs)) or 1.0
    df = Counter(t for d in docs for t in d)
    n = len(docs)

    # Score each record: the more rare, relevant keywords it shares with
    # the query, the higher it scores.
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
        # Only count it as a match if enough distinct words overlap.
        if shared >= MIN_SHARED_TERMS:
            scored.append((score, record))
    # Best match first; return the top `k`.
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:k]]


def format_for_prompt(query: str, exclude_run_id: str = None) -> str:
    """Turn the best matching past runs into a text block for the AI's
    prompt (empty if nothing similar was found)."""
    hits = recall(query, exclude_run_id)
    if not hits:
        return ""
    # One short bullet description per remembered run.
    blocks = []
    for r in hits:
        if r.get("kind") == SUMMARY_KIND:
            blocks.append(
                f"- {r['title']} -- outcomes: {r.get('outcome')}\n"
                f"  most often affected: {', '.join(r.get('affected_applications') or []) or 'none'}\n"
                f"  risks most often flagged: {'; '.join(_risks(r)) or 'n/a'}"
            )
            continue
        lines = [
            f"- {r.get('title') or r.get('run_id')} -- outcome: {r.get('outcome')}",
            f"  affected: {', '.join(r.get('affected_applications') or []) or 'none'}",
        ]
        if _risks(r):
            lines.append("  risks flagged: " + "; ".join(_risks(r)))
        blocks.append("\n".join(lines))
    body = "\n".join(blocks)
    return f"""
============================================================
SIMILAR PAST RUNS (memory -- reference only, NOT authoritative)
============================================================

Earlier runs on similar requirements. Use them to anticipate pitfalls
(especially the risks they flagged), but decide from THIS requirement
and the knowledge base; do not copy their scope.

{body}
"""
