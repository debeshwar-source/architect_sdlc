"""
The architecture knowledge bases: ONE markdown file per knowledge base, named after it
(knowledge_base/streamly.md). Deterministic, no LLM calls.

Plain words: each file is a crisp page describing a system. If it has an "## Applications" section whose
bullets look like  - `name` — what it does  then each bullet is one application; otherwise the whole page
is ONE application named after the file. The agents read the page itself; the applications list is what
the app uses to check names (an affected application must exist or be proposed as new) and to save
what a design adds: a new application is added to the list and gets its own section at the end of the
page, and an existing one gets a dated update section at the end. Existing text is never rewritten.

Knowledge bases are OPTIONAL: with none, the agents design from scratch. Writing is deliberately
conservative: a new knowledge base never reuses an existing name, a new application never overwrites an
existing one, and augmenting only ever appends. (Deciding WHETHER to write is the user's call - see
kb_save_node in orchestrator/graph.py.)
"""

import re
from datetime import datetime, timezone
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config


# A dataclass is a simple "record" type: a labelled bundle of facts about
# ONE application, filled in from its markdown document.
@dataclass
class ApplicationDoc:
    name: str
    path: Path                  # where the document lives on disk
    content: str                # the document's full text
    repo_url: Optional[str]     # link to the code repository, if stated
    purpose: Optional[str]      # one-line description of what it does


@lru_cache(maxsize=None)
def _field_pattern(label: str) -> "re.Pattern[str]":
    # Compiled once per label (the labels are few: "Repo", "Purpose").
    return re.compile(rf"\*\*{re.escape(label)}:\*\*\s*(.+(?:\n[ \t]+\S.*)*)")


def _extract_field(content: str, label: str) -> Optional[str]:
    # Finds a line such as "**Purpose:** does X" in the document and
    # returns what follows the label (or None if the label is absent).
    # (The pattern below is a "regular expression", a compact text-search rule.)
    # Captures the field's value across wrapped continuation lines
    # (indented lines immediately following, as this KB's markdown
    # uses), stopping at the first blank line or next bullet.
    match = _field_pattern(label).search(content)
    if not match:
        return None
    # Tidy up: squash extra whitespace/newlines and strip backticks.
    value = re.sub(r"\s+", " ", match.group(1)).strip().strip("`")
    return value


_APPLICATIONS_HEADING = re.compile(r"(?mi)^##\s+Applications\s*$")
_APP_BULLET = re.compile(r"^-\s+`([^`\n]+)`\s*(?:[—–:-]+\s*)?(.*)$")


def _applications_section(text: str) -> Optional[Tuple[int, int]]:
    """(start, end) character positions of the text after the '## Applications' heading, or None."""
    m = _APPLICATIONS_HEADING.search(text)
    if not m:
        return None
    nxt = re.search(r"(?m)^##\s", text[m.end():])
    return m.end(), (m.end() + nxt.start() if nxt else len(text))


# Parsed pages, keyed by file and remembered while the file is unchanged (its modified time and size are the
# check), so looking a knowledge base up several times in a run reads and parses it once.
_PARSED: Dict[Path, Tuple[Tuple[int, int], List["ApplicationDoc"]]] = {}


def load_all(kb_file: Path) -> List[ApplicationDoc]:
    """The applications of ONE knowledge base (a .md file). Each bullet in its '## Applications' section is one
    application; with no such section the whole page is one application named after the file."""
    try:
        st = kb_file.stat()
    except OSError:
        return []
    if not kb_file.is_file():
        return []
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _PARSED.get(kb_file)
    if hit and hit[0] == stamp:
        return list(hit[1])                      # a copy: callers may reorder or filter their list
    docs = _parse(kb_file)
    _PARSED[kb_file] = (stamp, docs)
    return list(docs)


def _parse(kb_file: Path) -> List[ApplicationDoc]:
    text = kb_file.read_text(encoding="utf-8")
    span = _applications_section(text)
    docs: List[ApplicationDoc] = []
    if span:
        current: Optional[List[str]] = None
        for line in text[span[0]:span[1]].splitlines():
            m = _APP_BULLET.match(line.strip()) if not line.startswith((" ", "\t")) else None
            if m:
                current = [m.group(1), m.group(2).strip(), line.strip()]
                docs.append(ApplicationDoc(m.group(1), kb_file, line.strip(), None, m.group(2).strip() or None))
            elif current and line.strip() and line.startswith((" ", "\t")):     # a wrapped continuation line
                last = docs[-1]
                docs[-1] = ApplicationDoc(last.name, kb_file, last.content + " " + line.strip(), None, last.purpose)
    if docs:
        return docs
    first = next((ln.strip() for ln in text.splitlines()[1:] if len(ln.strip()) > 40 and not ln.startswith(("#", "-", "|", "*", "<"))), "")
    return [ApplicationDoc(kb_file.stem, kb_file, text, _extract_field(text, "Repo"), _extract_field(text, "Purpose") or first or None)]


# ------------------------------------------------------------
# Finding and creating knowledge bases (the files themselves)
# ------------------------------------------------------------

@dataclass
class KnowledgeBase:
    name: str                # the file name without .md, e.g. "streamly"
    path: Path               # the file
    application_count: int   # how many applications it lists


def list_knowledge_bases(root: Path = None) -> List[KnowledgeBase]:
    """Every knowledge base under the root: each .md file is one. Files starting with "." or "_" are ignored.
    Returns an empty list if the root does not exist."""
    root = root or config.KNOWLEDGE_BASES_ROOT
    if not root.is_dir():
        return []
    return [
        KnowledgeBase(p.stem, p, len(load_all(p)))
        for p in sorted(root.glob("*.md"))
        if p.is_file() and not p.name.startswith((".", "_"))
    ]


def kb_path(name: str, root: Path = None) -> Path:
    """The file of the existing knowledge base called `name`. Only names that appear in list_knowledge_bases()
    are accepted, so a crafted name cannot point anywhere else."""
    for kb in list_knowledge_bases(root):
        if kb.name == name:
            return kb.path
    raise FileNotFoundError(f"No knowledge base named {name!r}")


def suggest_kb_name(title: str, root: Path = None) -> str:
    """A file-safe knowledge base name based on a project title, made unique
    among the existing ones (adds -2, -3 ... if the name is taken)."""
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40] or "new-knowledge-base"
    taken = {kb.name for kb in list_knowledge_bases(root)}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def normalise_kb_name(name: str) -> str:
    """The file name a typed knowledge-base name would become (lowercase
    letters, digits, hyphens). Empty if nothing usable is left."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def create_knowledge_base(name: str, root: Path = None) -> Path:
    """Create a NEW knowledge base: a page with an empty '## Applications' list. Refuses a name that is already
    taken - a "new" knowledge base must never be merged into an old one. The name is reduced to lowercase
    letters, digits and hyphens. Returns the file."""
    root = root or config.KNOWLEDGE_BASES_ROOT
    slug = _safe_name(name)
    path = root / f"{slug}.md"
    if path.exists():
        raise FileExistsError(f"A knowledge base named {slug!r} already exists")
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {slug}\n\n## Applications\n\n", encoding="utf-8")
    return path


# ------------------------------------------------------------
# Writing into one knowledge base
# ------------------------------------------------------------

def _safe_name(name: str) -> str:
    # Turn an application name into a safe name: lowercase letters, digits and hyphens only.
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not slug:
        raise ValueError(f"Invalid application name for the knowledge base: {name!r}")
    return slug


def canonical_name(name: str, known: List[str]) -> Optional[str]:
    """The spelling in `known` that `name` means, or None. Names are compared after reducing them to lowercase
    letters, digits and hyphens, so "d2c_user", "D2C-User" and "d2c-user" are the same application."""
    key = normalise_kb_name(name)
    if not key:
        return None
    return next((k for k in known if normalise_kb_name(k) == key), None)


def _purpose_of(content: str) -> str:
    """A one-line description for the applications list: the document's **Purpose:** field, else its first sentence."""
    field = _extract_field(content, "Purpose")
    if field:
        return field
    for line in content.splitlines():
        line = line.strip().lstrip("#>*- ").strip()
        if len(line) > 20:
            return line[:200]
    return ""


def create_doc(name: str, content: str, kb_file: Path) -> Path:
    """Add a NEW application: a bullet in the '## Applications' list and its own section at the end of the page.
    Refuses an application that is already listed (use augment_doc to add to it). Returns the file."""
    slug = _safe_name(name)
    if canonical_name(slug, [d.name for d in load_all(kb_file)]):
        raise FileExistsError(f"Knowledge base application already exists: {slug}")
    text = kb_file.read_text(encoding="utf-8") if kb_file.is_file() else f"# {kb_file.stem}\n"
    bullet = f"- `{slug}` — {_purpose_of(content)}".rstrip(" —")
    span = _applications_section(text)
    if span is None:
        implicit = load_all(kb_file) if kb_file.is_file() else []      # a page with no list is ONE application: keep it listed
        lines = [f"- `{d.name}` — {d.purpose or ''}".rstrip(" —") for d in implicit] + [bullet]
        text = text.rstrip() + "\n\n## Applications\n\n" + "\n".join(lines) + "\n"
    else:
        head, tail = text[:span[1]].rstrip(), text[span[1]:]
        text = head + "\n" + bullet + "\n" + ("\n" + tail if tail.strip() else "")
    text = text.rstrip() + f"\n\n## {slug}\n\n{content.strip()}\n"
    kb_file.write_text(text, encoding="utf-8")
    return kb_file


def augment_doc(name: str, addition: str, run_id: str = "", kb_file: Path = None) -> Path:
    """Append a dated section about an EXISTING application. Existing text is never changed or removed -- the
    addition goes at the end, labelled with the run that proposed it, so it is easy to review or revert."""
    slug = _safe_name(name)
    existing = canonical_name(slug, [d.name for d in load_all(kb_file)]) if kb_file is not None else None
    if existing is None:
        raise FileNotFoundError(f"Knowledge base application not found: {slug}")
    slug = existing                              # the heading uses the spelling the page already has
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source = f", run {run_id}" if run_id else ""
    with kb_file.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## Update to {slug} ({stamp}{source})\n\n{addition.strip()}\n")
    return kb_file


def apply_updates(
    updates: List[dict], run_id: str = "", kb_dir: Path = None, create_only: bool = False
) -> List[dict]:
    """Apply the Design agent's `knowledge_base_updates` list. Each item:
    {"application", "action": "create" | "augment", "content", "reason"}.
    With create_only=True (used for from-scratch builds) an application may only
    be CREATED: if the name is already listed, it is left untouched and the item is
    reported as skipped, so a from-scratch design can never be appended onto an unrelated one.
    `kb_dir` is the ONE knowledge-base file to write into (the name is kept for the callers).
    Never raises for one bad item - it records the problem and carries on,
    so a single bad update cannot lose the rest. Returns one result per
    item: {"application", "action", "status": "applied" | "skipped" |
    "failed", "path" / "detail"}."""
    if kb_dir is None:
        raise ValueError("apply_updates needs the knowledge-base file to write into")
    results = []
    for u in updates or []:
        name, action = u.get("application", ""), u.get("action", "")
        try:
            exists = canonical_name(_safe_name(name), [d.name for d in load_all(kb_dir)]) is not None
            if create_only and exists:
                results.append({
                    "application": name, "action": action, "status": "skipped",
                    "detail": "from-scratch build: an application with this name already exists "
                              "and was left untouched",
                })
                continue
            # Be forgiving about create-vs-augment: creating something that
            # already exists becomes an append; augmenting something that
            # does not exist becomes a create. Nothing is ever overwritten.
            if action == "create" and not exists:
                path = create_doc(name, u["content"], kb_dir)
                results.append({"application": name, "action": "create", "status": "applied", "path": str(path)})
            elif exists and action in ("create", "augment"):
                path = augment_doc(name, u["content"], run_id, kb_dir)
                results.append({"application": name, "action": "augment", "status": "applied", "path": str(path)})
            elif action == "augment":
                path = create_doc(name, u["content"], kb_dir)
                results.append({"application": name, "action": "create", "status": "applied", "path": str(path)})
            else:
                results.append({"application": name, "action": action, "status": "skipped", "detail": "unknown action"})
        except Exception as exc:  # noqa: BLE001 -- recorded, not fatal
            results.append({"application": name, "action": action, "status": "failed", "detail": str(exc)})
    return results


def application_names(docs: List[ApplicationDoc]) -> List[str]:
    # Just the list of names.
    return [doc.name for doc in docs]


# Shown to the agents INSTEAD of the knowledge base when the user asked for
# a from-scratch build. The existing documents are deliberately withheld, so
# the plan cannot be influenced by (or accidentally integrate with) them.
SCRATCH_KB_NOTICE = (
    "(This is a FROM-SCRATCH build. Any existing applications or knowledge "
    "base are intentionally NOT provided. Do not assume, mention, reuse or "
    "integrate with any existing system; design everything as new.)"
)

# Shown to the agents instead of the knowledge base when there is none.
EMPTY_KB_NOTICE = (
    "(The knowledge base is EMPTY -- there are no documented existing "
    "applications. Treat this as a greenfield effort: do not assume any "
    "existing system, and design whatever is needed from scratch.)"
)


def format_overview_for_prompt(docs: List[ApplicationDoc]) -> str:
    """Cheap, one-line-per-app summary -- for agents that just need to
    know what exists, not full detail (e.g. Requirement)."""
    # One bullet line per application: "- **name**: purpose".
    lines = [f"- **{doc.name}**: {doc.purpose or '(no purpose documented)'}" for doc in docs]
    return "\n".join(lines) if lines else EMPTY_KB_NOTICE


def format_full_kb_for_prompt(docs: List[ApplicationDoc]) -> str:
    """Full knowledge base text -- for agents that need to reason
    about dependencies, contracts, and constraints in detail (e.g.
    Architecture, Review)."""
    # Each application's whole document under its own heading, separated
    # by horizontal lines.
    sections = [f"### {doc.name}\n\n{doc.content}" for doc in docs]
    return "\n\n---\n\n".join(sections) if sections else EMPTY_KB_NOTICE


def get_doc(docs: List[ApplicationDoc], name: str) -> Optional[ApplicationDoc]:
    # Look up one application by name (None if not found).
    for doc in docs:
        if doc.name == name:
            return doc
    return None


_DEPENDS_ON = re.compile(r"## Depends on[^\n]*\n(.*?)(?=\n##|\Z)", re.DOTALL)
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def existing_dependency_edges(docs: List[ApplicationDoc]) -> List[Tuple[str, str]]:
    """(caller, callee) edges for the architecture as it exists TODAY,
    parsed from each doc's own "## Depends on" section -- only edges
    between two applications actually in this knowledge base; a
    dependency on an external system (a database, a third-party API)
    has no node in this graph to point at and is skipped. Used to
    render the "existing design" diagram alongside the Architecture
    Agent's proposed one. Deterministic text parsing, no LLM."""
    known_names = {doc.name for doc in docs}
    edges = []
    for doc in docs:
        # Find this document's "## Depends on" section...
        section_match = _DEPENDS_ON.search(doc.content)
        if not section_match:
            continue
        # ...and note every other known application mentioned in it (in `backticks`) as something
        # this one depends on. The names are read out of the section once, then looked up.
        for callee in dict.fromkeys(_BACKTICKED.findall(section_match.group(1))):
            if callee != doc.name and callee in known_names:
                edges.append((doc.name, callee))
    return edges


def kb_page(docs: List[ApplicationDoc]) -> str:
    """What the agents are shown of the knowledge base: the page itself (the .md file all the applications came
    from); if the documents do not come from one file, they are printed one after another."""
    if not docs:
        return EMPTY_KB_NOTICE
    paths = {d.path for d in docs}
    if len(paths) == 1:
        only = next(iter(paths))
        if only.is_file() and only.suffix == ".md":
            return only.read_text(encoding="utf-8").strip()
    return format_full_kb_for_prompt(docs)
