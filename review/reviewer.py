"""The code reviewer: reads a pull request's diff and proposes line-by-line review comments.

Plain words: turns the PR's diff into numbered text for the AI, asks for comments, then checks every comment against the diff so
that each one points at a line GitHub will accept (GitHub rejects a whole review if one comment is on a line outside the diff).
Nothing is posted here; the screen shows the comments to a person first.
"""

from typing import Any, Callable, Dict, List, Optional

import llm
from agents._common import load_skill
from coding import agent as coding_agent
from coding import diffview

MAX_DIFF_CHARS = 120_000          # beyond this, remaining files are left out of the review and listed as not reviewed
SNAP_DISTANCE = 3                 # a comment on a line just outside the diff is moved to the nearest commentable line within this
SEVERITIES = ("blocker", "suggestion", "nit")
BADGE = {"blocker": "🔴 Blocker", "suggestion": "🟡 Suggestion", "nit": "⚪ Nit"}


def commentable(files: List[diffview.FileDiff]) -> Dict[str, Dict[int, str]]:
    """{path: {new-file line number: text}} for every line of the new file that appears in the diff (added or unchanged context)."""
    out: Dict[str, Dict[int, str]] = {}
    for f in files:
        if f.status in ("deleted", "binary"):
            continue
        out[f.path] = {rno: rtext for kind, _l, _lt, rno, rtext in f.rows if kind != "sep" and rno is not None}
    return out


def numbered_diff(files: List[diffview.FileDiff], limit: int = MAX_DIFF_CHARS) -> tuple:
    """(text for the AI, [paths left out because of the size limit]). New-file lines carry their number; removed lines do not."""
    parts: List[str] = []
    used = 0
    skipped: List[str] = []
    for f in files:
        if f.status in ("deleted", "binary"):
            continue
        lines = [f"### {f.path}  ({f.status})"]
        for kind, lno, ltext, rno, rtext in f.rows:
            if kind == "sep":
                lines.append("   ...")
                continue
            if kind in ("del", "chg") and lno is not None:
                lines.append(f"     | -{ltext}")
            if rno is not None:
                lines.append(f"L{rno}| {'+' if kind in ('add', 'chg') else ' '}{rtext}")
        block = "\n".join(lines)
        if used + len(block) > limit:
            skipped.append(f.path)
            continue
        used += len(block)
        parts.append(block)
    return "\n\n".join(parts), skipped


def context_snippet(f: diffview.FileDiff, line: int, before: int = 4) -> str:
    """The commented line with the few lines above it, as git diff text (+ added, - removed, space unchanged)."""
    out: List[str] = []
    for i, row in enumerate(f.rows):
        if row[0] == "sep" or row[3] != line:
            continue
        for k, lno2, lt2, rno2, rt2 in f.rows[max(0, i - before):i + 1]:
            if k == "sep":
                out.append("   ⋯")
                continue
            if k in ("del", "chg") and lno2 is not None:
                out.append(f"      -  {lt2}")
            if rno2 is not None:
                out.append(f"{rno2:>5} {'+' if k in ('add', 'chg') else ' '}  {rt2}")
        break
    return "\n".join(out)


def compose_body(severity: str, comment: str, suggestion_code: str = "") -> str:
    """The text a comment will be posted with (what the person edits): severity tag, the remark, an optional GitHub suggestion block."""
    body = f"**{BADGE.get(severity, severity)}**: {comment.strip()}"
    if suggestion_code.strip("\n") and suggestion_code.strip():
        body += f"\n\n```suggestion\n{suggestion_code.rstrip()}\n```"
    return body


def _snap(line: Any, lines: Dict[int, str]) -> Optional[int]:
    try:
        line = int(line)
    except (TypeError, ValueError):
        return None
    if line in lines:
        return line
    near = [n for n in lines if abs(n - line) <= SNAP_DISTANCE]
    return min(near, key=lambda n: (abs(n - line), n)) if near else None


def _validate(result: Dict[str, Any]) -> None:
    if not isinstance(result.get("summary"), str) or not isinstance(result.get("comments"), list):
        raise ValueError("`summary` must be text and `comments` must be a list.")


def _already_open(threads: List[Dict[str, Any]], limit: int = 30) -> str:
    """Comments others already left on the PR, one short line each, so the reviewer does not say the same thing again."""
    lines = [f"- {t['path']}:{t['line']} (@{t['author']}): {' '.join(t['body'].split())[:200]}" for t in threads[:limit]]
    more = f"\n- ...and {len(threads) - limit} more" if len(threads) > limit else ""
    return "\n## Comments already open on this PR (written by others). Do NOT repeat these points; comment only on what they do not cover.\n" \
           + "\n".join(lines) + more + "\n"


def review(pr: Dict[str, Any], diff_text: str, model: Optional[str] = None, existing: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Review one PR. `existing` are the unresolved threads other people already left (never this app's own earlier ones, which the person
    may be about to delete). Returns {'summary', 'comments': [{'id','path','line','severity','body'}], 'notes': [...]} where every comment
    is anchored on a real line of the diff and `notes` lists anything the person should know (files left out, comments dropped)."""
    files = diffview.parse(diff_text)
    lines_by_path = commentable(files)
    notes: List[str] = []
    if not lines_by_path:
        return {"summary": "There is nothing to review: the diff has no added or changed text files.", "comments": [], "notes": notes}
    text, skipped = numbered_diff(files)
    if skipped:
        notes.append("Too large to review in one go, so these files were NOT reviewed: " + ", ".join(skipped))
    prompt = (f"# Pull request #{pr['number']}: {pr['title']}\nBranch {pr['branch']} -> {pr['base']}, by {pr['author']}\n\n"
              f"## Description\n{pr.get('body') or '(none)'}\n{_already_open(existing) if existing else ''}\n## Diff\n{text}")
    result = llm.call_llm_json(prompt, "code_review", required_keys=["summary", "comments"], system=load_skill("code_review"),
                               model=model, validate=_validate)
    comments: List[Dict[str, Any]] = []
    dropped = 0
    seen: set = set()
    for c in result["comments"]:
        anchored = _anchor(c, lines_by_path, seen, len(comments) + 1)
        if anchored:
            comments.append(anchored)
        else:
            dropped += 1
    if dropped:
        notes.append(f"{dropped} comment(s) pointed outside the changed code and were discarded.")
    comments.sort(key=lambda c: (c["path"], c["line"]))
    return {"summary": result["summary"].strip(), "comments": comments, "notes": notes}


def _anchor(raw: Any, lines_by_path: Dict[str, Dict[int, str]], seen: set, next_id: int) -> Optional[Dict[str, Any]]:
    """One AI comment, checked against the diff (see review): a comment dict, or None when it must be dropped."""
    path = raw.get("path") if isinstance(raw, dict) else None
    line = _snap(raw.get("line"), lines_by_path[path]) if path in lines_by_path else None
    remark = str(raw.get("comment") or "").strip() if isinstance(raw, dict) else ""
    if line is None or not remark or (path, line, remark) in seen:
        return None
    seen.add((path, line, remark))
    sev = raw.get("severity") if raw.get("severity") in SEVERITIES else "suggestion"
    sug = str(raw.get("suggestion_code") or "")
    if sug.strip() == lines_by_path[path][line].strip():
        sug = ""
    return {"id": f"c{next_id}", "path": path, "line": line, "severity": sev, "body": compose_body(sev, remark, sug),
            "remark": remark, "suggestion": sug, "evidence": str(raw.get("evidence") or "").strip()}


def recheck(pr: Dict[str, Any], threads: List[Dict[str, Any]], delta_diff: str, full_diff: str, next_id: int,
            model: Optional[str] = None, tree: Optional[str] = None, on_event: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """The reviewer's second look, after the coding agent answered the comments and pushed new commits.

    `threads`: the comments still waiting on the reviewer: [{'id','path','line','body','thread':[{'who','text'}]}].
    `delta_diff`: only what the new commits changed; `full_diff`: the whole PR (new comments must land on its lines).
    Returns {'summary', 'verdicts': [{'id','resolved','reply'}], 'comments': [new comments], 'notes'}."""
    lines_by_path = commentable(diffview.parse(full_diff))
    text, skipped = numbered_diff(diffview.parse(delta_diff))
    notes = ["Too large to re-check in one go; not re-checked: " + ", ".join(skipped)] if skipped else []
    asked = "\n\n".join(
        f"### Thread {t['id']}: {t['path']} line {t['line']}\n" +
        "\n".join(f"[{m['who']}] {m['text']}" for m in [{"who": "reviewer", "text": t["body"]}] + list(t.get("thread") or []))
        for t in threads)
    prompt = (f"# RE-CHECK of pull request #{pr['number']}: {pr['title']}\n\n## Threads waiting for your verdict\n{asked}\n\n"
              f"## What the new commits changed (the only code that is new since your review)\n{text or '(no change to the code)'}")
    if tree:
        if on_event:
            on_event("The reviewer is re-reading the code…")
        result = coding_agent._call("review-recheck", "reviewer", prompt, _system("code_review", "code_review_recheck", rules=RECHECK_RULES), tree,
                                    model, None, on_event, RECHECK_SCHEMA, _tools_for(pr), DISALLOWED, REVIEW_TURNS)["answer"]
    else:
        result = llm.call_llm_json(prompt, "code_review", required_keys=["summary", "verdicts", "comments"],
                                   system=load_skill("code_review") + "\n\n" + load_skill("code_review_recheck"), model=model,
                                   validate=lambda r: _validate({"summary": r.get("summary"), "comments": r.get("comments")}))
    ids = {t["id"] for t in threads}
    verdicts: Dict[str, Dict[str, Any]] = {}
    for v in result.get("verdicts") or []:
        if isinstance(v, dict) and v.get("id") in ids:
            verdicts[v["id"]] = {"id": v["id"], "resolved": bool(v.get("resolved")), "reply": str(v.get("reply") or "").strip()}
    for t in threads:                                  # a thread the reviewer did not judge stays open: never closed by silence
        verdicts.setdefault(t["id"], {"id": t["id"], "resolved": False, "reply": "Not re-checked; still open."})
    new, seen = [], set()
    for raw in result.get("comments") or []:
        c = _anchor(raw, lines_by_path, seen, next_id + len(new))
        if c:
            new.append(c)
    rejected: List[Dict[str, Any]] = []
    if tree and new:
        new, rejected = verify(pr, new, tree, model, on_event)
        if rejected:
            notes.append(f"{len(rejected)} new finding(s) did not survive a second look and were left out.")
    return {"summary": str(result["summary"]).strip(), "verdicts": list(verdicts.values()), "comments": new, "notes": notes,
            "rejected": rejected, "mode": "repo" if tree else "diff"}



# ---------- the reviewer with the repository in front of it (the way Claude Code reviews) ----------

REVIEW_TURNS = 80                 # reading files and searching takes many small steps
VERIFY_TURNS = 60
TOOLS = ["Read", "Glob", "Grep", "Bash"]          # no Edit or Write: the reviewer reads and runs things, it never changes the code
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]       # for a pull request from a fork: a stranger's code is read, never run here
# Git history, remotes and the network are off limits; everything else a developer would run to check a suspicion is allowed.
DISALLOWED = coding_agent.DISALLOWED + ["Bash(git fetch:*)", "Bash(git pull:*)", "Bash(git apply:*)", "Bash(git am:*)", "Bash(git restore:*)"]

EXPLORE_RULES = """
HOW TO WORK THIS TURN. The repository is your working directory, checked out at exactly the code of this pull request, so line numbers
in the diff below are line numbers in the files you can open. Review it the way a careful senior engineer would, not from the diff alone:
- Read each changed file around the changes, not only the changed lines. Follow the code outward: callers and callees of what changed,
  the types and interfaces it relies on, configuration, and the closest existing example of the same kind of code.
- Search (Grep, Glob) before you claim anything about the rest of the code: is a method really never called, is a value really never
  validated elsewhere, does a test really not cover this, does the convention really differ. A claim you did not check is a guess: leave it out.
- Look at the tests that came with the change and the ones that should have: do they assert the behavior, or only that code runs?
- You have a shell. Use it to verify (for example to run the project's unit tests or build, or a single test, if that is cheap and the project
  makes it possible), not to explore blindly. You cannot change files, and you must not try; git history, remotes and the network are off limits.
  Never run anything that needs credentials, a database or a service you were not given.
- You may see only part of the diff in the numbered listing below when it is large; the files listed as left out are yours to read yourself
  (`git diff <base>...HEAD -- <path>` works in the shell).
- For each comment give `evidence`: one or two sentences saying what you opened, searched or ran that shows it is true. A comment you cannot
  back with evidence does not belong in the answer.
Answer with JSON only, as the contract in your instructions says, with `evidence` added to every comment.
"""

EXPLORE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "comments": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "line": {"type": "integer"}, "severity": {"enum": list(SEVERITIES)},
            "comment": {"type": "string"}, "suggestion_code": {"type": "string"}, "evidence": {"type": "string"}}}},
    },
    "required": ["summary", "comments"],
}

RECHECK_RULES = """
HOW TO WORK THIS TURN. The repository (your working directory) is checked out at the pull request's code AFTER the coding agent's new commits.
For every thread: open the file at that place and read the code as it is now; follow what the fix calls and what calls it; check the test that
should cover it. Do not resolve a thread on the strength of the reply: resolve it because you saw the code is right. If a unit test settles the
question and is cheap to run, run it with your shell. You cannot change files; git history, remotes and the network are off limits.
Answer with JSON only, as the contract says; give `evidence` for every new comment.
"""

RECHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "verdicts": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "resolved": {"type": "boolean"}, "reply": {"type": "string"}}}},
        "comments": EXPLORE_SCHEMA["properties"]["comments"],
    },
    "required": ["summary", "verdicts", "comments"],
}


VERIFY_RULES = """
You are the second reviewer. Another reviewer proposed the findings below on this pull request. The repository (your working directory) is
checked out at the pull request's code. Your job is to CHECK each finding against the real code, not to find new ones: open the file, follow
the callers, search, run a test if that settles it. For each finding id decide:
- `confirmed: true` only if you verified it is real and matters for this change. If it is real but overstated or understated, correct the
  `severity` (blocker / suggestion / nit); if the wording is wrong or vague, give a better `comment` (1-3 sentences, specific).
- `confirmed: false` if it is wrong, already handled elsewhere, about code this change did not touch, a matter of taste, or you cannot
  verify it. Say why in `reason`, citing what you looked at.
Be sceptical: a false alarm costs the author's time, a missed real bug costs more. Give exactly one verdict per id. You cannot change files;
git history, remotes and the network are off limits.
Answer with JSON only: {"verdicts": [{"id": "c1", "confirmed": true, "reason": "...", "severity": "blocker|suggestion|nit", "comment": "..."}]}.
`severity` and `comment` are optional (omit to keep the original).
"""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "confirmed": {"type": "boolean"}, "reason": {"type": "string"},
        "severity": {"enum": list(SEVERITIES)}, "comment": {"type": "string"}}}}},
    "required": ["verdicts"],
}


def _system(*skills: str, rules: str = "") -> str:
    return "\n\n".join([load_skill(s) for s in skills] + ([rules.strip()] if rules else []))


def _tools_for(pr: Dict[str, Any]) -> List[str]:
    """The tools the reviewer gets for this PR: a shell only when the code comes from the repository's own branch."""
    fork = bool(pr.get("head_repo")) and pr.get("head_repo") != pr.get("base_repo")
    return READ_ONLY_TOOLS if fork else TOOLS


def verify(pr: Dict[str, Any], comments: List[Dict[str, Any]], tree: str, model: Optional[str] = None,
           on_event: Optional[Callable[[str], None]] = None) -> tuple:
    """A second look at proposed comments with the code in front of it. Returns (kept comments, rejected [{'path','line','body','reason'}]).
    A comment the verifier did not rule on is kept (it was not shown to be wrong), but is not marked as verified."""
    if not comments:
        return [], []
    listing = "\n\n".join(f"### {c['id']}: {c['path']} line {c['line']} [{c['severity']}]\n{c['remark']}"
                           + (f"\nSuggested replacement of that line: {c['suggestion']}" if c.get("suggestion") else "")
                           + (f"\nThe first reviewer's evidence: {c['evidence']}" if c.get("evidence") else "") for c in comments)
    prompt = f"# Pull request #{pr['number']}: {pr['title']}\nBranch {pr['branch']} -> {pr['base']}\n\n## Findings to check\n{listing}"
    result = coding_agent._call("review-verify", "reviewer", prompt, _system("code_review", rules=VERIFY_RULES), tree, model, None, on_event,
                                VERIFY_SCHEMA, _tools_for(pr), DISALLOWED, VERIFY_TURNS)
    verdicts = {v["id"]: v for v in result["answer"].get("verdicts") or [] if isinstance(v, dict) and v.get("id")}
    kept, rejected = [], []
    for c in comments:
        v = verdicts.get(c["id"])
        if v is None:
            kept.append({**c, "verified": False})
        elif not v.get("confirmed"):
            rejected.append({"path": c["path"], "line": c["line"], "body": c["body"], "reason": str(v.get("reason") or "").strip()})
        else:
            sev = v.get("severity") if v.get("severity") in SEVERITIES else c["severity"]
            remark = str(v.get("comment") or "").strip() or c["remark"]
            kept.append({**c, "severity": sev, "remark": remark, "body": compose_body(sev, remark, c.get("suggestion", "")),
                         "verified": True, "evidence": str(v.get("reason") or c.get("evidence") or "").strip()})
    return kept, rejected


def review_in_repo(pr: Dict[str, Any], diff_text: str, tree: str, model: Optional[str] = None, existing: Optional[List[Dict[str, Any]]] = None,
                   on_event: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Review one PR with the repository available: explore, then have every finding checked. Same result shape as `review`, plus
    'rejected' (findings the second look discarded, with its reason) and 'mode'."""
    files = diffview.parse(diff_text)
    lines_by_path = commentable(files)
    if not lines_by_path:
        return {"summary": "There is nothing to review: the diff has no added or changed text files.", "comments": [], "notes": [],
                "rejected": [], "mode": "repo"}
    text, skipped = numbered_diff(files)
    notes = ["Not in the listing the reviewer was given (too large), so it had to read these itself: " + ", ".join(skipped)] if skipped else []
    prompt = (f"# Pull request #{pr['number']}: {pr['title']}\nBranch {pr['branch']} -> {pr['base']}, by {pr['author']}\n\n"
              f"## Description\n{pr.get('body') or '(none)'}\n{_already_open(existing) if existing else ''}\n## Diff (new-file line numbers)\n{text}")
    if "Bash" not in _tools_for(pr):
        prompt += ("\n\nNOTE: this pull request comes from a fork, so its code is not run here: you have NO shell this time. Read and search "
                   "only, and say in `evidence` what you read.")
        notes.append("This pull request comes from a fork, so the reviewer only read and searched its code; it did not run anything.")
    if on_event:
        on_event("The reviewer is reading the code…")
    result = coding_agent._call("review", "reviewer", prompt, _system("code_review", rules=EXPLORE_RULES), tree, model, None, on_event,
                                EXPLORE_SCHEMA, _tools_for(pr), DISALLOWED, REVIEW_TURNS)["answer"]
    comments, seen, dropped = [], set(), 0
    for raw in result.get("comments") or []:
        c = _anchor(raw, lines_by_path, seen, len(comments) + 1)
        if c:
            comments.append(c)
        else:
            dropped += 1
    if dropped:
        notes.append(f"{dropped} comment(s) pointed outside the changed code and were discarded.")
    rejected: List[Dict[str, Any]] = []
    if comments:
        if on_event:
            on_event(f"A second look is checking {len(comments)} finding(s) against the code…")
        comments, rejected = verify(pr, comments, tree, model, on_event)
        if rejected:
            notes.append(f"{len(rejected)} finding(s) did not survive a second look at the code and were left out (see below).")
        if any(not c.get("verified") for c in comments):
            notes.append("Some findings could not be confirmed or rejected by the second look; they are marked as unchecked.")
    comments.sort(key=lambda c: (c["path"], c["line"]))
    return {"summary": str(result.get("summary") or "").strip(), "comments": comments, "notes": notes, "rejected": rejected, "mode": "repo"}


# ---------- entry points: the checkout, and a visible fallback ----------

def _tree_or_note(owner: str, name: str, number: int, on_event: Optional[Callable[[str], None]]) -> tuple:
    """(folder of the PR's code or None, a note for the person when it could not be had)."""
    from review import checkout
    try:
        if on_event:
            on_event("Fetching the pull request's code…")
        return str(checkout.prepare(owner, name, number)), ""
    except checkout.CheckoutError as exc:
        return None, ("The reviewer could NOT check out the repository, so it reviewed from the diff alone (it could not open other files, "
                      f"search, or run tests). Why: {exc}")


def review_pr(owner: str, name: str, pr: Dict[str, Any], diff_text: str, model: Optional[str] = None,
              existing: Optional[List[Dict[str, Any]]] = None, on_event: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Review a PR with everything a developer has: the code checked out, files to read, a shell, and a second look at every finding. If
    the code cannot be fetched it falls back to the diff alone and says so in `notes`; it never silently reviews with less."""
    tree, note = _tree_or_note(owner, name, pr["number"], on_event)
    if tree:
        return review_in_repo(pr, diff_text, tree, model, existing, on_event)
    out = review(pr, diff_text, model=model, existing=existing)
    return {**out, "notes": [note] + out["notes"], "rejected": [], "mode": "diff"}
