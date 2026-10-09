"""
Loads the pre-existing architecture knowledge base: one .md file per
known application/repo. Deterministic, no LLM calls.

Unlike ai_native_sdlc's context_layer (which live-scans a single
cloned repo), this reads curated documents describing MANY
applications the org already maintains -- there is no repo to clone
at this layer, only the docs that describe the architecture.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import config


@dataclass
class ApplicationDoc:
    name: str
    path: Path
    content: str
    repo_url: Optional[str]
    purpose: Optional[str]


def _extract_field(content: str, label: str) -> Optional[str]:
    # Captures the field's value across wrapped continuation lines
    # (indented lines immediately following, as this KB's markdown
    # uses), stopping at the first blank line or next bullet.
    match = re.search(
        rf"\*\*{re.escape(label)}:\*\*\s*(.+(?:\n[ \t]+\S.*)*)",
        content,
    )
    if not match:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip().strip("`")
    return value


def load_all(kb_dir: Path = config.KNOWLEDGE_BASE_DIR) -> List[ApplicationDoc]:
    if not kb_dir.is_dir():
        return []

    docs = []
    for path in sorted(kb_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8")
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


def application_names(docs: List[ApplicationDoc]) -> List[str]:
    return [doc.name for doc in docs]


def format_overview_for_prompt(docs: List[ApplicationDoc]) -> str:
    """Cheap, one-line-per-app summary -- for agents that just need to
    know what exists, not full detail (e.g. Requirement)."""
    lines = [f"- **{doc.name}**: {doc.purpose or '(no purpose documented)'}" for doc in docs]
    return "\n".join(lines) if lines else "(no applications in the knowledge base)"


def format_full_kb_for_prompt(docs: List[ApplicationDoc]) -> str:
    """Full knowledge base text -- for agents that need to reason
    about dependencies, contracts, and constraints in detail (e.g.
    Architecture, Review)."""
    sections = [f"### {doc.name}\n\n{doc.content}" for doc in docs]
    return "\n\n---\n\n".join(sections) if sections else "(no applications in the knowledge base)"


def get_doc(docs: List[ApplicationDoc], name: str) -> Optional[ApplicationDoc]:
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
        section_match = re.search(r"## Depends on[^\n]*\n(.*?)(?=\n##|\Z)", doc.content, re.DOTALL)
        if not section_match:
            continue
        section = section_match.group(1)
        for callee in known_names:
            if callee != doc.name and re.search(rf"`{re.escape(callee)}`", section):
                edges.append((doc.name, callee))
    return edges
