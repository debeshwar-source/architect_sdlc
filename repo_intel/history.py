"""Git history and decision evidence. Rationale is only reported when a commit body or ADR states it."""

import re
from collections import Counter
from pathlib import Path
from typing import List, Optional

from .ingest import git, IngestError
from .security import redact

SEP, REC = "\x1f", "\x1e"
DECISION_WORDS = re.compile(r"(?i)\b(migrat\w*|replac\w*|switch\w*|introduc\w*|adopt\w*|deprecat\w*|revert\w*|"
                            r"instead of|rather than|because|in order to|so that|refactor\w*|drop\w*|remov\w*)\b")
WHY_WORDS = re.compile(r"(?i)\b(because|since|so that|in order to|instead of|rather than|due to|to avoid|to allow|why)\b")
ADR_DIRS = ("docs/adr", "doc/adr", "adr", "docs/decisions", "decisions", "docs/architecture/decisions")


def commits(root: Path, max_commits: int = 500, since: Optional[str] = None) -> List[dict]:
    rng = f"{since}..HEAD" if since else "HEAD"
    try:
        raw = git(["log", rng, f"--max-count={max_commits}", "--no-merges", "--numstat",
                   f"--format={REC}%H{SEP}%an{SEP}%aI{SEP}%s{SEP}%b{SEP}"], root)
    except IngestError:
        return []
    out = []
    for rec in raw.split(REC)[1:]:
        head, _, stat = rec.partition("\n")
        parts = (head + "\n" + stat).split(SEP)
        if len(parts) < 6:
            continue
        h, an, date, subj, body, tail = parts[0], parts[1], parts[2], parts[3], parts[4], SEP.join(parts[5:])
        files, added, deleted = [], 0, 0
        for line in tail.splitlines():
            m = re.match(r"(\d+|-)\t(\d+|-)\t(.+)", line)
            if m:
                files.append(m.group(3))
                added += int(m.group(1)) if m.group(1) != "-" else 0
                deleted += int(m.group(2)) if m.group(2) != "-" else 0
        out.append({"sha": h, "author": an, "date": date[:10], "subject": redact(subj), "body": redact(body.strip()),
                    "files": files, "added": added, "deleted": deleted})
    return out


def significant(commits_: List[dict], limit: int = 25) -> List[dict]:
    """Commits worth recording: decision-flavoured wording, a stated reason, or unusually large change."""
    scored = []
    for c in commits_:
        score = (2 if DECISION_WORDS.search(c["subject"]) else 0) + (2 if WHY_WORDS.search(c["body"]) else 0) \
            + (1 if len(c["body"]) > 200 else 0) + (1 if c["added"] + c["deleted"] > 500 else 0)
        if score >= 2:
            c = {**c, "score": score, "rationale_stated": bool(WHY_WORDS.search(c["body"]))}
            scored.append(c)
    return sorted(scored, key=lambda c: (-c["score"], c["date"]))[:limit]


def churn(commits_: List[dict], top: int = 15) -> List[tuple]:
    cnt = Counter(f for c in commits_ for f in c["files"])
    return cnt.most_common(top)


def find_adrs(files) -> List[dict]:
    out = []
    for f in files:
        low = f.path.lower()
        if f.kind == "doc" and any(low.startswith(d + "/") or f"/{d}/" in low for d in ADR_DIRS) \
                and not low.endswith(("readme.md", "template.md")):
            out.append(f.path)
    return out


def parse_adr(text: str, path: str) -> dict:
    title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("#")), path)
    m = re.search(r"(?im)^\W*status\W*[:\n]\s*([^\n]+)", text)
    sections = {}
    for name in ("context", "decision", "consequences", "alternatives", "rationale"):
        sm = re.search(rf"(?ims)^#+\s*{name}[^\n]*\n(.*?)(?=^#+\s|\Z)", text)
        if sm:
            sections[name] = redact(" ".join(sm.group(1).split())[:500])
    return {"path": path, "title": redact(title[:150]), "status": (m.group(1).strip()[:40] if m else "unknown"),
            "sections": sections}
