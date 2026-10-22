"""Side-by-side diff: the old file on the left, the new one on the right, removed lines red and added lines green.

Plain words: takes the text `git diff` prints and turns it into one HTML page (see render) that Streamlit can show.
Nothing here runs Git or the AI; it only reads text, so it is easy to test.
"""

import html
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

MAX_ROWS_PER_FILE = 2500          # a huge generated file is cut here, with a note

# A row is (kind, left_no, left_text, right_no, right_text); kind: ctx | chg | del | add | sep
Row = Tuple[str, Optional[int], str, Optional[int], str]

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_FILE = re.compile(r"^diff --git a/(.*) b/(.*)$")


@dataclass
class FileDiff:
    path: str
    old_path: str = ""
    status: str = "modified"          # modified | added | deleted | renamed | binary
    rows: List[Row] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    truncated: bool = False


def _flush(f: FileDiff, removed: List[Tuple[int, str]], added: List[Tuple[int, str]]) -> None:
    """Put a block of removed lines next to the added lines that replace them."""
    for i in range(max(len(removed), len(added))):
        left = removed[i] if i < len(removed) else None
        right = added[i] if i < len(added) else None
        kind = "chg" if left and right else "del" if left else "add"
        f.rows.append((kind, left[0] if left else None, left[1] if left else "",
                       right[0] if right else None, right[1] if right else ""))
    f.removed += len(removed)
    f.added += len(added)
    removed.clear()
    added.clear()


def parse(diff_text: str) -> List[FileDiff]:
    files: List[FileDiff] = []
    cur: Optional[FileDiff] = None
    old_left = new_left = 0               # lines still to read in the current hunk
    ln = rn = 0
    removed: List[Tuple[int, str]] = []
    added: List[Tuple[int, str]] = []
    first_hunk = True
    for line in diff_text.splitlines():
        if old_left <= 0 and new_left <= 0:                  # between hunks: a header line
            m = _FILE.match(line)
            if m:
                if cur:
                    _flush(cur, removed, added)
                cur = FileDiff(path=m.group(2), old_path=m.group(1))
                files.append(cur)
                first_hunk = True
                continue
            if cur is None:
                continue
            if line.startswith("new file mode"):
                cur.status = "added"
            elif line.startswith("deleted file mode"):
                cur.status = "deleted"
            elif line.startswith("rename from "):
                cur.status, cur.old_path = "renamed", line[len("rename from "):]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.status = "binary"
            else:
                h = _HUNK.match(line)
                if h:
                    _flush(cur, removed, added)
                    ln, rn = int(h.group(1)), int(h.group(3))
                    old_left = int(h.group(2)) if h.group(2) is not None else 1
                    new_left = int(h.group(4)) if h.group(4) is not None else 1
                    if not first_hunk or ln > 1:
                        cur.rows.append(("sep", None, h.group(5) or "", None, ""))
                    first_hunk = False
            continue
        if line.startswith("\\"):                            # "\ No newline at end of file"
            continue
        if line.startswith("-"):
            removed.append((ln, line[1:]))
            ln += 1
            old_left -= 1
        elif line.startswith("+"):
            added.append((rn, line[1:]))
            rn += 1
            new_left -= 1
        else:
            _flush(cur, removed, added)
            cur.rows.append(("ctx", ln, line[1:], rn, line[1:]))
            ln += 1
            rn += 1
            old_left -= 1
            new_left -= 1
    if cur:
        _flush(cur, removed, added)
    for f in files:
        if len(f.rows) > MAX_ROWS_PER_FILE:
            f.rows, f.truncated = f.rows[:MAX_ROWS_PER_FILE], True
    return files


CSS = """
body{margin:0;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#1f2328;background:#fff}
.file{border:1px solid #d0d7de;border-radius:6px;margin:0 0 14px;overflow:hidden}
.head{background:#f6f8fa;padding:6px 10px;border-bottom:1px solid #d0d7de;font-weight:600}
.head .add{color:#1a7f37}.head .del{color:#cf222e}.head .tag{font-weight:400;color:#57606a;margin-left:8px}
table{border-collapse:collapse;width:100%;table-layout:fixed}
td{padding:0 6px;white-space:pre;overflow:hidden;vertical-align:top}
td.n{width:42px;text-align:right;color:#8c959f;background:#f6f8fa;user-select:none}
td.t{width:calc(50% - 42px)}
td.t.del,td.n.del{background:#ffebe9}td.t.add,td.n.add{background:#dafbe1}
td.t.chg-l{background:#ffebe9}td.t.chg-r{background:#dafbe1}
td.t.empty{background:#f6f8fa}
tr.sep td{background:#ddf4ff;color:#57606a;text-align:left}
td.t.l{border-right:1px solid #d0d7de}
.note{padding:8px 10px;color:#57606a}
"""


def _cell(no: Optional[int], text: str, cls: str, side: str) -> str:
    n = "" if no is None else str(no)
    return f'<td class="n {cls}">{n}</td><td class="t {cls} {side}">{html.escape(text)}</td>'


def render(files: List[FileDiff]) -> str:
    """One self-contained HTML document showing every file side by side."""
    if not files:
        return f"<style>{CSS}</style><div class='note'>No changes.</div>"
    out = [f"<style>{CSS}</style>"]
    for f in files:
        title = html.escape(f.path if f.status != "renamed" else f"{f.old_path} → {f.path}")
        out.append(f'<div class="file"><div class="head">{title}<span class="tag">{f.status}</span> '
                   f'<span class="add">+{f.added}</span> <span class="del">−{f.removed}</span></div>')
        if f.status == "binary":
            out.append("<div class='note'>Binary file; not shown.</div></div>")
            continue
        if f.status == "renamed" and not f.rows:
            out.append("<div class='note'>Renamed without changes.</div></div>")
            continue
        out.append("<table>")
        for kind, lno, ltext, rno, rtext in f.rows:
            if kind == "sep":
                out.append(f'<tr class="sep"><td colspan="4">⋯ {html.escape(ltext)}</td></tr>')
            elif kind == "ctx":
                out.append(f"<tr>{_cell(lno, ltext, '', 'l')}{_cell(rno, rtext, '', 'r')}</tr>")
            else:
                left = _cell(lno, ltext, "del" if kind in ("del", "chg") else "empty", "l") if lno is not None else \
                    '<td class="n empty"></td><td class="t empty l"></td>'
                right = _cell(rno, rtext, "add", "r") if rno is not None else '<td class="n empty"></td><td class="t empty r"></td>'
                out.append(f"<tr>{left}{right}</tr>")
        out.append("</table>")
        if f.truncated:
            out.append(f"<div class='note'>Showing the first {MAX_ROWS_PER_FILE} rows of this file.</div>")
        out.append("</div>")
    return "".join(out)


def height(files: List[FileDiff], cap: int = 900) -> int:
    """A sensible frame height for Streamlit: grows with the diff, then scrolls."""
    rows = sum(len(f.rows) for f in files) + 3 * len(files)
    return max(80, min(cap, rows * 18 + 40))


def summarize(files: List[FileDiff]) -> List[Tuple[str, str, int, int]]:
    """(path, status, added, removed) for each file, for the 'files changed' list."""
    return [(f.path, f.status, f.added, f.removed) for f in files]
