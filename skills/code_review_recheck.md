# One-pager: code_review_recheck

**This turn: RE-CHECK.** You reviewed this pull request earlier and the coding agent has since answered your comments and pushed new commits. For every thread listed, decide if it is settled. You see the thread (your comment, then the coding agent's reply) and the code the new commits changed. When you also have the repository (you will be told), open the code at each thread's place and confirm the fix really works; do not take the reply's word for it.

**Rules**
- `resolved: true` only if the new code really fixes the problem, or the coding agent's reasoning for NOT changing it is correct. A reply that says "fixed" with no matching change in the diff is NOT resolved.
- `resolved: false` when the problem remains, the fix is wrong or incomplete, or it introduced a new problem; then `reply` says in 1-2 sentences exactly what is still wrong. When `resolved: true`, `reply` is one short sentence (for example "Fixed, thanks.").
- Give one verdict for EVERY thread, using its id exactly (`c1`...). Do not argue a point you already lost: if the coding agent's explanation is sound, resolve it.
- Look at the changed code for NEW problems the fix introduced; put them in `comments` (same shape as a first review: `path`, `line`, `severity`, `comment`, `suggestion_code`). Do not raise new points about code that was already there. Usually `comments` is `[]`.
- Line numbers and rules for `comments` are as in a first review.
- `summary`: 1-3 sentences: how many threads are settled, what remains, and your verdict (ready to merge / needs another round).

## Output contract

Return one valid JSON object and nothing else:

{
  "summary": "",
  "verdicts": [{"id": "c1", "resolved": true, "reply": ""}],
  "comments": [{"path": "", "line": 0, "severity": "blocker|suggestion|nit", "comment": "", "suggestion_code": ""}]
}
