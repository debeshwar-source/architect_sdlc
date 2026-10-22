# repo_intel — repository → engineering knowledge base

Turns a Git repository into a folder of linked Markdown documents with evidence labels, provenance metadata and
incremental updates. This is an **MVP**: the deterministic core is implemented and tested; the items under
[Limitations](#limitations-assumptions-risks) are not.

## Usage

```bash
# from the project root (needs only git + Python 3.11+; --llm additionally needs claude_agent_sdk, already in requirements.txt)
python -m repo_intel.cli build https://github.com/org/repo.git     # first run: creates knowledge_base/repo/
python -m repo_intel.cli build https://github.com/org/repo.git     # later run: updates knowledge_base/repo/ in place
python -m repo_intel.cli build . --ref v1.2                        # local repo, tag/branch/SHA
GH_TOKEN=... python -m repo_intel.cli build https://github.com/org/private.git --token-env GH_TOKEN
python -m repo_intel.cli build URL --exclude 'legacy/*' --work-dir .clone --llm
python -m repo_intel.cli build URL --kb-root /other/library        # different library folder (--out FOLDER = exact folder)
python -m pytest test_repo_intel.py                                                 # 32 tests
```

**Web UI:** `streamlit run repo_intel_app.py` (Build form, result/quality/review view, page browser with Mermaid diagrams).

Exit code is 0 for `ok` / `completed-with-validation-errors`, 1 for `failed`. A worked example generated from this very
repository is in [example_output/](example_output/README.md).

## Where knowledge bases are stored

Each repository gets one sub-folder of `knowledge_base/` (override with `--kb-root`, or `CLAUDE_SDLC_ARCHITECT_KB_ROOT`).
The folder is named after the repository and is re-found by the repository address stored in
`metadata/source-index.json`, so rebuilding (even with a different `--ref`, or a differently-spelled URL such as
`git@github.com:o/r.git` vs `https://github.com/o/r`) updates the same folder. A different repository with the same name gets
`name-<hash>`. Building another ref of the same repository overwrites that folder's content (one folder = one repository).

## How the SDLC pipeline uses these knowledge bases

These folders appear in the SDLC app's knowledge-base selector like any other (`knowledge_base/loader.py` →
[sdlc_adapter.py](sdlc_adapter.py)). At load time each component page becomes one application document
(`**Repo:**`, `**Tech stack:**`, `**Purpose:**`, `## Depends on` built from the verified import graph, plus the full code
analysis with its evidence labels), and a `<repo>-system` document carries the stack, dependency map, decisions and
rationale, configuration, build/test, debt signals and unresolved questions. Nothing extra is stored. Purpose lines say
where they came from ("from module docstring", "inferred"); a component with no stated purpose shows as undocumented
rather than being guessed. Updates the pipeline saves after user approval land as top-level files in the same folder;
they are appended to the matching component, survive rebuilds, and are not touched by `repo_intel`.

## Architecture

```
 source ─▶ ingest ─▶ discover ─▶ analyze ─▶ [synthesize?] ─▶ generate ─▶ merge/write ─▶ validate ─▶ report
 (URL/path) git, ref   ls-files    ast/regex   optional LLM    Model→MD    manual-safe    links,refs   metadata/
            pinned     classify    manifests   evidence-gated  builders    atomic         dupes
            SHA        hash        components                              
                                   git history ─────────────────▶ decisions, churn, change history
```

| Module | Responsibility |
|---|---|
| [ingest.py](ingest.py) | clone/fetch into an isolated dir, pin ref → commit SHA, auth via env-injected header, hooks/`ext::` disabled, partial-clone cleanup |
| [discover.py](discover.py) | `git ls-files` (so `.gitignore` holds), exclusions, binary/generated/oversize/symlink handling, file classification, SHA-256 |
| [analyze.py](analyze.py) | Python `ast` symbols with line ranges, env-var reads, TODO markers, JS import edges, manifests → dependencies, stack detection, component grouping and import graph |
| [history.py](history.py) | commits, churn, ADR discovery/parsing, "decision-flavoured" commit selection |
| [generate.py](generate.py) | one builder per document; returns `None` when there is nothing to say (no placeholders) |
| [synthesize.py](synthesize.py) | optional LLM summaries, fenced as untrusted data, dropped unless they cite real files |
| [pipeline.py](pipeline.py) | orchestration, caches, manual-block merge, stale handling, review flags, report |
| [validate.py](validate.py) | link integrity, source-reference validity, duplicate paragraphs, orphan pages |
| [security.py](security.py) | secret redaction, injection heuristics, input validation |

## Output structure (adaptive)

Written under `--out`: `README.md` (index) plus only those of `repository-overview/`, `architecture/`,
`applications/<component>/`, `engineering-practices/`, `decisions-and-rationale/`, `operations/`,
`quality-and-reliability/`, `dependency-catalog/`, `change-history/`, `data-and-contracts/`, `metadata/` that the
repository gives evidence for. Components are nested-manifest directories (monorepo units), else top-level folders;
the 40 largest get a page. `metadata/` holds `manifest.json` (doc fingerprints), `source-index.json` (per-file hash,
kind, component) and `generation-report.json`. `.cache/` (analysis and LLM caches) is safe to delete.

## Evidence convention

Every statement carries one of `[Verified]`, `[Documented]`, `[Inferred]`, `[Recommendation]`, `[Unknown]`. Source
references use `` `path:L10-20` `` and are machine-checked. Commit rationale is `[Documented]` only when the commit
body contains reasoning words; otherwise `[Unknown]`. Alternatives are listed only if an ADR names them. LLM text is
always `[Inferred]`.

## Update semantics

1. Re-acquire (fetch + checkout); compute file hashes; diff against `source-index.json`.
2. Unchanged files reuse cached analysis (keyed by content hash); LLM output is cached by prompt hash.
3. All documents are re-rendered deterministically (cheap); only content that differs is written, so unchanged
   runs are byte-identical (timestamps live only in `metadata/`).
4. Text inside the `notes` manual block of any page is carried over. If the *generated* part of a page was edited by
   hand, the file is left alone, the new version is written as `<name>.proposed.md`, and the page is listed under
   `needs_review`.
5. Pages whose component vanished are deleted if untouched, otherwise prefixed `> **STALE:**`.
6. Added/removed/modified manifest, CI, infra, migration, API-spec files and anything with auth/secret/token-like
   names are listed under `needs_review`.

## Key decisions and trade-offs

| Decision | Why | Cost / revisit when |
|---|---|---|
| Deterministic core, LLM optional | Reproducible, testable, no hallucinated facts; LLM only adds labelled summaries | Shallower prose; revisit once an eval set exists for LLM claims |
| `ast` for Python, regex for others | Zero dependencies, never executes code | JS/TS/Go/Java symbol extraction is missing; adopt tree-sitter |
| Re-render everything, write only diffs | Simple and correct; avoids a fragile dependency tracker | Fine to ~10⁵ files; LLM stage would need per-component invalidation at scale |
| Clone locally even for local paths | Analyses exactly the committed state, leaves the user's tree alone | Uncommitted changes are not documented |
| Token via `http.extraHeader` env | Never in URL, config or disk | HTTPS only; SSH relies on the user's agent |
| Human edits win | Never silently overwrite | Proposals need manual reconciliation |

## Phased plan

| Phase | Scope | Acceptance |
|---|---|---|
| 1 (done) | ingest, discover, Python/manifest analysis, history, 14 doc types, incremental, validation, CLI | `pytest test_repo_intel.py` green; example output validates |
| 2 | tree-sitter for TS/JS/Go/Java; route/endpoint, DB-model and event extraction; OpenAPI/proto summaries; workflow & sequence diagrams | per-language fixtures; contract pages list real endpoints |
| 3 | PR/issue ingestion (GitHub/GitLab API); LLM per-component pages with claim verification and an eval set | claims traceable; precision measured on labelled repos |
| 4 | job queue, per-repo locks, scheduled/webhook runs, metrics, sandboxed optional build/test introspection | concurrent runs safe; SLOs defined |

## Operations

* **Deploy:** run the CLI from cron/CI. Persist `--out` (commit it to a docs repo or store on a volume) so runs stay
  incremental; `--work-dir` keeps the clone between runs and speeds fetches. Without it a temp clone is made and deleted.
* **Configure:** `--ref`, `--exclude`, `--max-commits`, `--token-env`. Limits in code: 1 MB per file, 20 000 files,
  40 component pages, 500 commits.
* **Troubleshoot:** read `metadata/generation-report.json` (`errors`, `warnings`, `needs_review`, `quality`). "git clone
  failed" → URL/token; "ref not found" → fetch happened but the name is neither branch, tag nor commit; validation
  errors list the exact document and reference.
* **Recover:** every step is idempotent; just re-run. A crash leaves the previous manifest, and documents are written
  via temp-file rename. Delete `.cache/` to force full re-analysis; delete `metadata/manifest.json` to treat every
  existing page as user-owned (proposals will be written instead of overwrites).

## Limitations, assumptions, risks

* Deep analysis is **Python only**; other languages get file/import/env/TODO-level facts. Components' "responsibility"
  is not stated unless a docstring says so or `--llm` is used.
* Business rules, invariants, data-flow, sequence diagrams, failure modes and troubleshooting pages are **not yet
  generated**; they need Phase 2–3 extraction. `[Unknown]` is reported rather than guessed.
* Test linkage is import-based reachability, not coverage. Dynamic imports/reflection are invisible.
* PR/issue discussions are not fetched; decision evidence is ADRs, commit messages and code only. Squash-merge repos
  with terse messages will show mostly `[Unknown]` rationale.
* Secret redaction and prompt-injection detection are heuristics. Treat the output as internal until reviewed.
* No concurrency control: do not run two builds against one `--out` at once.
* Commit history is windowed (`--max-commits`) and fully re-read each run.
* The LLM path (`--llm`) is covered only with fakes in tests; it has not been run against a live model here.
