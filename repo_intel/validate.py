"""Quality checks over a generated knowledge base: links, source references, coverage, duplicates."""

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
SRCREF_RE = re.compile(r"`([\w./@+-]+\.\w+):L(\d+)(?:-(\d+))?`")
FILEREF_RE = re.compile(r"`([\w./@+-]*/[\w./@+-]+\.\w+|[\w@+-]+\.(?:py|js|ts|tsx|go|java|rs|rb|toml|json|ya?ml|md|txt|sh))`")
MIN_DUP_LEN = 90


def validate(kb: Path, repo: Path, line_counts: Optional[Dict[str, int]] = None, only: Optional[set] = None) -> dict:
    """`only`: restrict to these documents (others in the folder, e.g. files the SDLC pipeline saved, are not ours to judge)."""
    docs = {p.relative_to(kb).as_posix(): p.read_text(encoding="utf-8") for p in kb.rglob("*.md")
            if ".proposed" not in p.name and (only is None or p.relative_to(kb).as_posix() in only)}
    broken, bad_refs, n_links, n_refs = [], [], 0, 0
    para_owner = defaultdict(set)
    for name, text in docs.items():
        for target in LINK_RE.findall(text):
            if re.match(r"[a-z]+://|mailto:", target):
                continue
            n_links += 1
            path, _, anchor = target.partition("#")
            if path and not (kb / Path(name).parent / path).resolve().is_file():
                broken.append({"doc": name, "target": target})
        for path, a, b in SRCREF_RE.findall(text):
            n_refs += 1
            fp = repo / path
            if not fp.is_file():
                bad_refs.append({"doc": name, "ref": f"{path}:L{a}", "reason": "file not found"})
                continue
            n = (line_counts or {}).get(path) or len(fp.read_text(encoding="utf-8", errors="replace").splitlines())
            if int(a) > n or (b and int(b) > n):
                bad_refs.append({"doc": name, "ref": f"{path}:L{a}" + (f"-{b}" if b else ""), "reason": f"file has {n} lines"})
        for path in FILEREF_RE.findall(text):
            if not (repo / path).is_file() and not path.startswith(("http", ".", "metadata/")) and "/" in path:
                bad_refs.append({"doc": name, "ref": path, "reason": "file not found"})
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            if len(para) >= MIN_DUP_LEN and not para.startswith(("|", "```", "#", "- ", "**Evidence labels", "`[Verified]` Commands", ">")):
                para_owner[para].add(name)
    dups = [{"docs": sorted(d), "text": p[:100]} for p, d in para_owner.items() if len(d) > 1]
    linked = {str((Path(n).parent / t.partition("#")[0]).as_posix()) for n, tx in docs.items() for t in LINK_RE.findall(tx) if not re.match(r"[a-z]+://", t)}
    norm = {re.sub(r"[^/]+/\.\./", "", x) for x in linked}
    orphans = sorted(d for d in docs if d != "README.md" and d not in norm)
    return {"documents": len(docs), "links_checked": n_links, "broken_links": broken,
            "source_refs_checked": n_refs, "invalid_source_refs": bad_refs,
            "duplicate_paragraphs": dups, "orphan_documents": orphans,
            "passed": not broken and not bad_refs}
