"""File discovery and classification. Uses `git ls-files` so .gitignore is respected by construction."""

import fnmatch
import hashlib
from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
from typing import List, Optional

from .ingest import git

MAX_FILE_BYTES = 1_000_000
DEFAULT_EXCLUDES = ["node_modules/*", "*/node_modules/*", "vendor/*", "*/vendor/*", "dist/*", "build/*",
                    "*.min.js", "*.min.css", "*.map", ".git/*", "*/__pycache__/*", "*.snap", "target/*"]
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "Pipfile.lock",
             "go.sum", "composer.lock", "Gemfile.lock", "uv.lock"}
MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "go.mod", "Cargo.toml",
             "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "Pipfile"}
LANGS = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".ts": "TypeScript",
         ".tsx": "TypeScript", ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".rs": "Rust", ".rb": "Ruby",
         ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".swift": "Swift",
         ".scala": "Scala", ".sh": "Shell", ".sql": "SQL", ".tf": "Terraform", ".vue": "Vue"}
CODE_LANGS = set(LANGS.values()) - {"SQL", "Terraform", "Shell"}
TEXT_EXT = set(LANGS) | {".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".proto",
                         ".graphql", ".gql", ".env", ".properties", ".gradle", ".html", ".css", ""}


@dataclass
class FileInfo:
    path: str
    size: int
    sha256: str
    language: Optional[str]
    kind: str          # source test config doc migration api-spec infra ci script lockfile manifest other
    analyze: bool      # False: listed and hashed but not parsed (lockfile, generated, too large...)
    skip_reason: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def classify(path: str) -> str:
    p = PurePosixPath(path)
    name, parts, ext = p.name, [x.lower() for x in p.parts], p.suffix.lower()
    low = path.lower()
    if name in LOCKFILES:
        return "lockfile"
    if name in MANIFESTS:
        return "manifest"
    if ".github/workflows" in low or name in (".gitlab-ci.yml", "Jenkinsfile", ".travis.yml", "azure-pipelines.yml") \
            or ".circleci" in parts:
        return "ci"
    if name.startswith("Dockerfile") or name.startswith("docker-compose") or ext == ".tf" \
            or any(x in ("k8s", "kubernetes", "helm", "terraform", "charts") for x in parts):
        return "infra"
    if any(x in ("migrations", "migrate", "alembic") for x in parts) or "db/migrate" in low:
        return "migration"
    if ext in (".proto", ".graphql", ".gql") or any(k in name.lower() for k in ("openapi", "swagger")):
        return "api-spec"
    if name.startswith("test_") or name.endswith(("_test.py", "_test.go", ".test.js", ".test.ts", ".test.tsx",
                                                  ".spec.js", ".spec.ts", "Test.java", "_spec.rb")) \
            or any(x in ("tests", "test", "__tests__", "spec") for x in parts[:-1]) or name == "conftest.py":
        return "test"
    if ext in (".md", ".rst") or name.upper().startswith(("README", "CHANGELOG", "CONTRIBUTING", "LICENSE")):
        return "doc"
    if ext == ".sh" or (parts and parts[0] in ("scripts", "bin") and ext in ("", ".py", ".sh")):
        return "script"
    if ext in LANGS:
        return "source"
    if ext in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".properties", ".xml"):
        return "config"
    return "other"


def _excluded(path: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def discover(root: Path, extra_excludes: Optional[List[str]] = None, max_bytes: int = MAX_FILE_BYTES) -> List[FileInfo]:
    patterns = DEFAULT_EXCLUDES + list(extra_excludes or [])
    out = git(["ls-files", "-z"], root).split("\0")
    files: List[FileInfo] = []
    for rel in sorted(x for x in out if x):
        fp = root / rel
        if fp.is_symlink() or not fp.is_file():    # never follow symlinks out of the clone
            continue
        size = fp.stat().st_size
        kind = classify(rel)
        lang = LANGS.get(PurePosixPath(rel).suffix.lower())
        reason = None
        if _excluded(rel, patterns):
            reason = "excluded by pattern"
        elif kind == "lockfile":
            reason = "lockfile"
        elif size > max_bytes:
            reason = f"larger than {max_bytes} bytes"
        data = b"" if reason == "excluded by pattern" else fp.read_bytes()[:max_bytes + 1]
        if reason is None:
            if b"\0" in data[:8192]:
                reason = "binary"
            elif PurePosixPath(rel).suffix.lower() not in TEXT_EXT and kind == "other":
                reason = "unrecognised file type"
            elif any(m in data[:512] for m in (b"@generated", b"DO NOT EDIT", b"Code generated by", b"auto-generated")):
                reason = "generated file"
        sha = hashlib.sha256(fp.read_bytes() if size <= max_bytes else data).hexdigest() if reason != "excluded by pattern" else ""
        files.append(FileInfo(rel, size, sha, lang, kind, reason is None, reason))
    return files
