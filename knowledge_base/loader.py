"""
Loads the pre-existing architecture knowledge base: one .md file per
known application/repo. Deterministic, no LLM calls.

Unlike ai_native_sdlc's context_layer (which live-scans a single
cloned repo), this reads curated documents describing MANY
applications the org already maintains -- there is no repo to clone
at this layer, only the docs that describe the architecture.

Plain words: reads the library of short documents that describe each
existing software application (what it does, what it depends on) so the
AI agents can read them. No AI is used here - it only reads files.

There can be SEVERAL knowledge bases: each sub-folder of
config.KNOWLEDGE_BASES_ROOT is one (e.g. knowledge_base/streamly/), holding one
.md file per application. A run uses one chosen knowledge base, or none.
Knowledge bases are OPTIONAL: with none (or an empty one), load_all() returns
an empty list and the agents design from scratch.
This module can also WRITE (create_knowledge_base / create_doc / augment_doc),
which is how a design adds to a knowledge base or starts a new one. Writing is
deliberately conservative: a new knowledge base never reuses an existing
name, a new document never overwrites an existing one, and augmenting only
ever appends. (Deciding WHETHER to write is the user's call - see
kb_save_node in orchestrator/graph.py.)
"""

import re
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

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


def _extract_field(content: str, label: str) -> Optional[str]:
    # Finds a line such as "**Purpose:** does X" in the document and
    # returns what follows the label (or None if the label is absent).
    # (The pattern below is a "regular expression", a compact text-search rule.)
    # Captures the field's value across wrapped continuation lines
    # (indented lines immediately following, as this KB's markdown
    # uses), stopping at the first blank line or next bullet.
    match = re.search(
        rf"\*\*{re.escape(label)}:\*\*\s*(.+(?:\n[ \t]+\S.*)*)",
        content,
    )
    if not match:
        return None
    # Tidy up: squash extra whitespace/newlines and strip backticks.
    value = re.sub(r"\s+", " ", match.group(1)).strip().strip("`")
    return value


def load_all(kb_dir: Path) -> List[ApplicationDoc]:
    """Read every .md file in ONE knowledge-base folder, one per application."""
    # Folder missing -> no applications.
    if not kb_dir.is_dir():
        return []

    docs = []
    # Go through the files in alphabetical order for consistent results.
    for path in sorted(kb_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        # The application's name is the file name without ".md".
        name = path.stem
        docs.append(
            ApplicationDoc(
                name=name,
                path=path,
                content=content,
                repo_url=_extract_field(content, "Repo"),
                purpose=_extract_field(content, "Purpose"),
            )
        )
    return docs


# ------------------------------------------------------------
# Finding and creating knowledge bases (the folders themselves)
# ------------------------------------------------------------

@dataclass
class KnowledgeBase:
    name: str                # the folder name, e.g. "streamly"
    path: Path               # where it lives
    application_count: int   # how many application documents it holds


def list_knowledge_bases(root: Path = None) -> List[KnowledgeBase]:
    """Every knowledge base under the root: each sub-folder is one. Hidden and
    internal folders (starting with "." or "_", like __pycache__) are ignored.
    Returns an empty list if the root does not exist."""
    root = root or config.KNOWLEDGE_BASES_ROOT
    if not root.is_dir():
        return []
    return [
        KnowledgeBase(p.name, p, len(list(p.glob("*.md"))))
        for p in sorted(root.iterdir())
        if p.is_dir() and not p.name.startswith((".", "_"))
    ]


def kb_path(name: str, root: Path = None) -> Path:
    """The folder of the existing knowledge base called `name`. Only names that
    appear in list_knowledge_bases() are accepted, so a crafted name cannot
    point anywhere else."""
    for kb in list_knowledge_bases(root):
        if kb.name == name:
            return kb.path
    raise FileNotFoundError(f"No knowledge base named {name!r}")


def suggest_kb_name(title: str, root: Path = None) -> str:
    """A folder-safe knowledge base name based on a project title, made unique
    among the existing ones (adds -2, -3 ... if the name is taken)."""
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40] or "new-knowledge-base"
    taken = {kb.name for kb in list_knowledge_bases(root)}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def normalise_kb_name(name: str) -> str:
    """The folder name a typed knowledge-base name would become (lowercase
    letters, digits, hyphens). Empty if nothing usable is left."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def create_knowledge_base(name: str, root: Path = None) -> Path:
    """Create a NEW, empty knowledge base. Refuses a name that is already
    taken - a "new" knowledge base must never be merged into an old one.
    The name is reduced to lowercase letters, digits and hyphens."""
    root = root or config.KNOWLEDGE_BASES_ROOT
    path = root / _safe_name(name)
    if path.exists():
        raise FileExistsError(f"A knowledge base named {path.name!r} already exists")
    path.mkdir(parents=True)
    return path


# ------------------------------------------------------------
# Writing documents into one knowledge base
# ------------------------------------------------------------

def _safe_name(name: str) -> str:
    # Turn an application name into a safe file name: lowercase letters,
    # digits and hyphens only. This also blocks tricks like "../../etc"
    # that would otherwise write outside the knowledge-base folder.
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not slug:
        raise ValueError(f"Invalid application name for the knowledge base: {name!r}")
    return slug


def create_doc(name: str, content: str, kb_dir: Path) -> Path:
    """Create a NEW application document. Refuses to overwrite an
    existing one (use augment_doc to add to it). Creates the folder if
    it does not exist yet. Returns the path written."""
    path = kb_dir / f"{_safe_name(name)}.md"
    if path.exists():
        raise FileExistsError(f"Knowledge base document already exists: {path.name}")
    kb_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path


def augment_doc(name: str, addition: str, run_id: str = "", kb_dir: Path = None) -> Path:
    """Append a dated section to an EXISTING document. Existing text is
    never changed or removed -- the addition goes at the end, labelled
    with the run that proposed it, so it is easy to review or revert."""
    path = kb_dir / f"{_safe_name(name)}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Knowledge base document not found: {path.name}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source = f", run {run_id}" if run_id else ""
    block = f"\n\n## Update ({stamp}{source})\n\n{addition.strip()}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(block)
    return path


def apply_updates(
    updates: List[dict], run_id: str = "", kb_dir: Path = None, create_only: bool = False
) -> List[dict]:
    """Apply the Design agent's `knowledge_base_updates` list. Each item:
    {"application", "action": "create" | "augment", "content", "reason"}.
    With create_only=True (used for from-scratch builds) a document may only
    be CREATED: if the name is already taken by an existing document, that
    document is left untouched and the item is reported as skipped, so a
    from-scratch design can never be appended onto an unrelated old document.
    `kb_dir` is the ONE knowledge-base folder to write into.
    Never raises for one bad item - it records the problem and carries on,
    so a single bad update cannot lose the rest. Returns one result per
    item: {"application", "action", "status": "applied" | "skipped" |
    "failed", "path" / "detail"}."""
    if kb_dir is None:
        raise ValueError("apply_updates needs the knowledge-base folder to write into")
    results = []
    for u in updates or []:
        name, action = u.get("application", ""), u.get("action", "")
        try:
            exists = (kb_dir / f"{_safe_name(name)}.md").is_file()
            if create_only and exists:
                results.append({
                    "application": name, "action": action, "status": "skipped",
                    "detail": "from-scratch build: a document with this name already exists "
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
        section_match = re.search(r"## Depends on[^\n]*\n(.*?)(?=\n##|\Z)", doc.content, re.DOTALL)
        if not section_match:
            continue
        section = section_match.group(1)
        # ...and note every other known application mentioned in it (in
        # `backticks`) as something this one depends on.
        for callee in known_names:
            if callee != doc.name and re.search(rf"`{re.escape(callee)}`", section):
                edges.append((doc.name, callee))
    return edges
