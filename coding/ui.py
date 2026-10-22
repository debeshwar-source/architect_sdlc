"""The coding agent's screen, usable on its own (coding_app.py) or inside the main app (streamlit_app.py).

Plain words: pick a task and a repository. Engineering Manager 2 explores the repository (searching and opening files as it needs
them) and breaks the task into exact subtasks, each given to the best expert; you can ask it to reconsider, then freeze them, then allow (or not) that Jira
is updated with them. Each subtask is then carried out on request, shown to you as a side-by-side diff, and either
approved (committed locally) or sent back with your comments. When every one is done you see the whole change and may
raise the pull request.
"""

from typing import List, Optional

import html as _html
import streamlit as st
import streamlit.components.v1 as components

import config
from ui_kit import dropdown
from agents.catalog import NEEDS_HUMAN, ROLES
from coding import board, diffview, flow, memory, testrun
from coding import tasks as task_list
from coding import workspace as ws

AGENTS = list(ROLES)
EM2 = "Engineering Manager 2 — explores the repository (recommended)"
EM1 = "The first manager's subtasks, as they are"
ICON = {"pending": "⚪", "running": "🔄", "testreview": "🧪", "results": "📊", "review": "🟡", "accepted": "🟢", "asking": "❓", "blocked": "⛔", "approved": "✅", "skipped": "⏭️"}


STAGES = [("planning", "🧭 Engineering Manager 2"), ("jira", "🗂️ Jira"), ("executing", "🛠️ Coding agent"), ("final", "🔀 Pull request")]
WHAT = ("**Engineering Manager 2** explores the repository and breaks the task into exact subtasks, each for the right expert → "
        "you reconsider and freeze them → you decide whether Jira is updated → the **coding agent** carries them out one by one "
        "(you review each diff) → you raise the **pull request**.")


CSS = """
<style>
/* ---- the pieces we draw ourselves ---- */
.sd-stepper{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:.2rem 0 1rem}
.sd-chip{display:inline-flex;align-items:center;gap:8px;padding:7px 16px;border-radius:999px;font-size:.88rem;font-weight:600;
  border:1px solid rgba(128,128,128,.3);opacity:.55;transition:all .25s ease}
.sd-chip .dot{width:21px;height:21px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:.72rem;
  background:rgba(128,128,128,.25)}
.sd-chip.done{opacity:1;border-color:rgba(34,197,94,.55);background:linear-gradient(135deg,rgba(34,197,94,.16),rgba(34,197,94,.06))}
.sd-chip.done .dot{background:#22c55e;color:#fff}
.sd-chip.now{opacity:1;border-color:#6366f1;background:linear-gradient(135deg,rgba(99,102,241,.24),rgba(139,92,246,.12));
  box-shadow:0 4px 18px rgba(99,102,241,.28)}
.sd-chip.now .dot{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;animation:sd-pulse 1.8s ease-in-out infinite}
.sd-arrow{opacity:.35}
@keyframes sd-pulse{0%{box-shadow:0 0 0 0 rgba(99,102,241,.55)}70%{box-shadow:0 0 0 9px rgba(99,102,241,0)}100%{box-shadow:0 0 0 0 rgba(99,102,241,0)}}
@keyframes sd-rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

.sd-hero{position:relative;overflow:hidden;border-radius:20px;padding:20px 24px 18px;margin:.2rem 0 1rem;color:#fff;
  background:linear-gradient(120deg,#4f46e5 0%,#7c3aed 55%,#c026d3 120%);box-shadow:0 12px 34px rgba(79,70,229,.32);animation:sd-rise .45s ease both}
.sd-hero:after{content:"";position:absolute;right:-60px;top:-60px;width:220px;height:220px;border-radius:50%;background:rgba(255,255,255,.1)}
.sd-hero-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.sd-hero-title{font-size:1.25rem;font-weight:700;letter-spacing:-.01em;flex:1;min-width:240px}
.sd-mode{background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.35);padding:4px 12px;border-radius:999px;font-size:.8rem;font-weight:700;
  backdrop-filter:blur(4px)}
.sd-hero-meta{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 14px}
.sd-tag{background:rgba(0,0,0,.2);padding:3px 11px;border-radius:999px;font-size:.78rem;font-weight:500}
.sd-segs{display:flex;gap:5px}
.sd-seg{flex:1;height:9px;border-radius:999px;background:rgba(255,255,255,.22)}
.sd-seg.done{background:#4ade80}
.sd-seg.skip{background:rgba(255,255,255,.5)}
.sd-seg.now{background:#fde047;animation:sd-glow 1.6s ease-in-out infinite}
@keyframes sd-glow{0%,100%{opacity:1}50%{opacity:.45}}
.sd-hero-sub{margin-top:8px;font-size:.8rem;opacity:.9;font-weight:500}

.sd-row{display:flex;align-items:center;gap:12px;padding:11px 16px;margin:8px 0;border-radius:14px;border:1px solid rgba(128,128,128,.25);
  transition:transform .2s ease,box-shadow .2s ease;animation:sd-rise .4s ease both}
.sd-row:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(79,70,229,.14)}
.sd-row.now{border-color:#6366f1;background:linear-gradient(135deg,rgba(99,102,241,.12),rgba(139,92,246,.05));box-shadow:0 6px 24px rgba(99,102,241,.2)}
.sd-row .n{width:28px;height:28px;border-radius:9px;display:inline-flex;align-items:center;justify-content:center;font-weight:700;
  background:rgba(128,128,128,.2);font-size:.82rem;flex:none}
.sd-row.now .n{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.sd-row .t{flex:1;font-weight:600}
.sd-row .who{font-size:.72rem;opacity:.6;white-space:nowrap}
.sd-row code{font-size:.75rem}
.sd-pill{padding:3px 12px;border-radius:999px;font-size:.74rem;font-weight:700;white-space:nowrap}
.sd-pill.grey{background:rgba(128,128,128,.2)}
.sd-pill.blue{background:rgba(99,102,241,.2);color:#818cf8}
.sd-pill.amber{background:rgba(245,158,11,.2);color:#d97706}
.sd-pill.green{background:rgba(34,197,94,.2);color:#16a34a}
.sd-pill.red{background:rgba(239,68,68,.2);color:#dc2626}
.sd-bar{display:flex;height:14px;border-radius:999px;overflow:hidden;background:rgba(128,128,128,.2);margin:.3rem 0 .8rem;box-shadow:inset 0 1px 3px rgba(0,0,0,.15)}
.sd-bar span{display:block;height:100%;transition:width .6s ease}
.sd-legend{font-size:.8rem;opacity:.8;margin-bottom:.6rem}
.sd-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 12px}

/* ---- Streamlit's own widgets, inside the coding screen only ---- */
div[class*="sdroot"] button[kind="primary"],div[class*="sdroot"] button[data-testid="stBaseButton-primary"]{
  background:linear-gradient(135deg,#4f46e5,#7c3aed);border:0;color:#fff;font-weight:600;box-shadow:0 4px 14px rgba(79,70,229,.35);
  transition:transform .15s ease,box-shadow .15s ease,filter .15s ease}
div[class*="sdroot"] button[kind="primary"]:hover,div[class*="sdroot"] button[data-testid="stBaseButton-primary"]:hover{
  transform:translateY(-2px);box-shadow:0 8px 22px rgba(79,70,229,.45);filter:brightness(1.07)}
div[class*="sdroot"] button[kind="primary"]:active{transform:translateY(0)}
div[class*="sdroot"] button[kind="secondary"],div[class*="sdroot"] button[data-testid="stBaseButton-secondary"]{
  border-radius:12px;font-weight:500;transition:all .15s ease}
div[class*="sdroot"] button[kind="secondary"]:hover,div[class*="sdroot"] button[data-testid="stBaseButton-secondary"]:hover{
  border-color:#6366f1;color:#6366f1;transform:translateY(-1px)}
div[class*="sdroot"] div[data-testid="stMetric"]{border:1px solid rgba(128,128,128,.25);border-radius:16px;padding:12px 16px;
  background:linear-gradient(160deg,rgba(99,102,241,.07),rgba(128,128,128,.03));box-shadow:0 2px 10px rgba(0,0,0,.05)}
div[class*="sdroot"] div[data-testid="stMetricValue"]{font-weight:700;letter-spacing:-.02em}
div[class*="sdroot"] div[data-testid="stMetricLabel"]{opacity:.7}
div[class*="sdroot"] div[data-testid="stExpander"] details{border-radius:14px;border:1px solid rgba(128,128,128,.25);
  transition:border-color .2s ease,box-shadow .2s ease}
div[class*="sdroot"] div[data-testid="stExpander"] details:hover{border-color:rgba(99,102,241,.55);box-shadow:0 4px 16px rgba(99,102,241,.1)}
div[class*="sdroot"] div[data-testid="stExpander"] summary{font-weight:600}
div[class*="sdroot"] div[data-testid="stAlert"]{border-radius:14px;border:1px solid rgba(128,128,128,.2);animation:sd-rise .35s ease both}
div[class*="sdroot"] div[data-testid="stVerticalBlockBorderWrapper"]{border-radius:16px}
div[class*="sdroot"] div[data-testid="stCode"],div[class*="sdroot"] div[data-testid="stDataFrame"]{border-radius:12px;overflow:hidden}
div[class*="sdroot"] button[data-baseweb="tab"]{font-weight:600}
div[class*="sdroot"] div[data-baseweb="tab-highlight"]{background:linear-gradient(90deg,#4f46e5,#7c3aed);height:3px;border-radius:3px}
div[class*="sdroot"] textarea,div[class*="sdroot"] input[type="text"]{border-radius:12px}
div[class*="sdroot"] h2,div[class*="sdroot"] h3{letter-spacing:-.01em}
div[class*="sdroot"] hr{border:0;height:1px;background:linear-gradient(90deg,transparent,rgba(128,128,128,.4),transparent)}
</style>
"""

PILL = {"pending": "grey", "running": "blue", "review": "amber", "testreview": "amber", "results": "amber", "accepted": "blue",
        "asking": "amber", "blocked": "red", "approved": "green", "skipped": "grey"}
PIPE = ["Code", "Tests", "Report", "Commit"]


def _style() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def _chips(items: list) -> str:
    """items: (label, state) with state done | now | todo."""
    out = []
    for i, (label, state) in enumerate(items):
        dot = "✓" if state == "done" else str(i + 1)
        out.append(f"<span class='sd-chip {state}'><span class='dot'>{dot}</span>{_html.escape(label)}</span>")
    return "<div class='sd-stepper'>" + "<span class='sd-arrow'>›</span>".join(out) + "</div>"


def _stage_strip(phase: str) -> None:
    """Where the job is: finished stages ticked, the current one lit, the rest dimmed."""
    order = [p for p, _ in STAGES]
    here = order.index("final" if phase == "done" else phase)
    st.markdown(_chips([(label.split(" ", 1)[1] if " " in label else label, "done" if i < here or phase == "done" else "now" if i == here else "todo")
                        for i, (_, label) in enumerate(STAGES)]), unsafe_allow_html=True)


def _pipeline(step: dict) -> None:
    """Where one subtask is: code, tests, report, commit."""
    at = {"pending": 0, "running": 0 if step.get("phase", "code") == "code" else 1, "asking": 0, "blocked": 0, "review": 0, "testreview": 1,
          "results": 2, "accepted": 3, "approved": 4, "skipped": 4}.get(step["status"], 0)
    if step["status"] == "running" and step.get("test_report"):
        at = 2
    st.markdown(_chips([(label, "done" if i < at else "now" if i == at and step["status"] != "skipped" else "todo")
                        for i, label in enumerate(PIPE)]), unsafe_allow_html=True)


def _step_rows(job: flow.Job, current: Optional[dict]) -> None:
    rows = []
    for s in job.steps:
        commit = f"<code>{s['commit'][:8]}</code>" if s.get("commit") else ""
        now = " now" if current is not None and s["n"] == current["n"] else ""
        who = f"<span class='who'>{_html.escape(s.get('agent') or '')}</span>" if s.get("agent") else ""
        rows.append(f"<div class='sd-row{now}'><span class='n'>{s['n']}</span><span class='t'>{_html.escape(s['title'])}</span>{who}{commit}"
                    f"<span class='sd-pill {PILL.get(s['status'], 'grey')}'>{ICON.get(s['status'], '')} {STATUS_LABEL.get(s['status'], s['status'])}</span></div>")
    st.markdown("".join(rows), unsafe_allow_html=True)


def _result_bar(r: dict) -> None:
    total = max(r.get("total") or 0, 1)
    parts = [(r.get("passed", 0), "#22c55e", "passed"), (r.get("failed", 0), "#ef4444", "failed"), (r.get("skipped", 0), "#9ca3af", "skipped")]
    bar = "".join(f"<span style='width:{100 * n / total:.1f}%;background:{c}'></span>" for n, c, _ in parts if n)
    legend = "".join(f"<i style='background:{c}'></i>{n} {name}" for n, c, name in parts)
    st.markdown(f"<div class='sd-bar'>{bar}</div><div class='sd-legend'>{legend}</div>", unsafe_allow_html=True)


def _undo(label: str, key: str, blocker: str, action, confirm: str = "", *args) -> None:
    """One task-specific undo button. Disabled, with the reason beside it, when that task cannot be undone now. A task that reaches outside
    this computer (a pushed branch, a pull request) first asks for a tick in `confirm`. `action(job, ...)` runs, errors are shown."""
    sure = True
    if confirm and not blocker:
        sure = st.checkbox(confirm, key=key + "_ok")
    if st.button(f"↶ {label}", key=key, disabled=bool(blocker) or not sure, help=blocker or "Undo only this task"):
        try:
            action(*args)
        except (ValueError, ws.GitError) as exc:
            st.error(str(exc))
            return
        st.rerun()
    if blocker:
        st.caption(f"Cannot be undone now: {blocker}")


def _live(status):
    box = status.empty()
    return lambda text: box.write("• " + text)


def _run(label: str, fn, *args, **kwargs) -> None:
    """Run one agent turn with a live list of what it is doing, then redraw."""
    with st.status(label, expanded=True) as status:
        fn(*args, on_event=_live(status), **kwargs)
        status.update(label="Done.", state="complete")
    st.rerun()


def show_diff(diff_text: str, key: str) -> None:
    files = diffview.parse(diff_text)
    if not files:
        st.info("No changes.")
        return
    st.caption(f"{len(files)} file(s) changed · left: before · right: after (red = removed, green = added)")
    components.html(diffview.render(files), height=diffview.height(files), scrolling=True)


def render(tasks: List[task_list.CodingTask], key: str = "coding_", sidebar: bool = True) -> None:
    """Draw the whole screen: Engineering Manager 2 first; the coding agent below it (it has work only once the subtasks are frozen
    and Jira answered). `key` keeps the widgets and state apart from the rest of the page."""
    _style()
    with st.container(key=key + "sdroot"):                      # the look below applies inside this container only
        dropdown("🧭 Engineering Manager 2", key + "dd_em2", lambda: _manager_section(tasks, key, sidebar))
        job = flow.load(st.session_state.get(key + "job") or "")
        if job and job.phase in ("executing", "final", "done"):             # only once the subtasks are frozen
            dropdown("🛠️ Coding agent", key + "dd_coding", lambda: _coding_section(key + job.id + "_", job))


def _manager_section(tasks: List[task_list.CodingTask], key: str, sidebar: bool) -> Optional[flow.Job]:
    """Pick the task and repository, then Engineering Manager 2's subtasks, your review, and the Jira question."""
    k = key
    holder = st.sidebar if sidebar else st.container(border=True)
    with holder:
        st.header("Task")
        chosen = st.selectbox("Task from the Engineering Manager", tasks, format_func=lambda t: f"{t.run_id[:15]} · {t.label}",
                              index=None, key=k + "task", placeholder="No release plan found" if not tasks else "Choose a task…")
        default_agent = chosen.agent if chosen and chosen.agent in ROLES else None
        if default_agent != st.session_state.get(k + "default"):          # follow the manager's pick when the task changes
            st.session_state[k + "default"] = default_agent
            st.session_state[k + "agent"] = default_agent
        agent = st.selectbox("Default expert agent", AGENTS, index=None, key=k + "agent", placeholder="Choose an expert…",
                             help="Pre-selected from the first manager's assignment. Engineering Manager 2 gives each subtask its own expert; this one is used when it does not.")
        if chosen and chosen.agent == NEEDS_HUMAN:
            st.caption("The manager marked this task as needing a person; pick the expert you want to try.")
        picked: List[int] = []
        use_em2 = True
        if chosen:
            mode = st.radio("Break the task into subtasks with", [EM2, EM1], key=k + "mode" + chosen.key,
                            index=0,
                            help="Engineering Manager 2 explores the repository as needed and writes exact instructions, so the coding agent "
                                 "does not have to. The other option uses the first manager's short subtasks as they are.")
            use_em2 = mode == EM2
            if not use_em2 and chosen.subtasks:
                picked = st.multiselect("Subtasks to execute, in this order", list(range(len(chosen.subtasks))),
                                        default=list(range(len(chosen.subtasks))), key=k + "subs_" + chosen.key,
                                        format_func=lambda i: f"{i + 1}. {chosen.subtasks[i]['title']}")
            elif not use_em2:
                st.caption("This task has no subtasks from the first manager; use Engineering Manager 2.")
        st.header("Repository")
        source = st.text_input("GitHub address or local folder", key=k + "source",
                               placeholder="https://github.com/org/repo.git  or  /path/to/repo",
                               help="A local folder is never edited directly: the agent works in a separate worktree on a new branch.")
        base = st.text_input("Base branch (optional)", key=k + "base",
                             help="The branch the pull request targets. Default: the current branch of a local folder, or the repository's default branch.")
        model = st.selectbox("Model", config.AVAILABLE_MODELS, key=k + "model",
                             index=config.AVAILABLE_MODELS.index(config.MODEL_ID) if config.MODEL_ID in config.AVAILABLE_MODELS else 0)
        how = st.radio("How should the coding agent work?", ["Autonomous", "Step by step"], key=k + "how",
                       help="Autonomous: the agent codes, writes the tests, runs them and fixes failures by itself, like Claude Code; you review "
                            "everything once at the end. Step by step: you approve the code, then the tests, then the report.")
        go = st.button("Start a new job", type="primary", key=k + "start", use_container_width=True,
                       disabled=not (chosen and agent and source.strip() and (use_em2 or picked)))
        saved = flow.list_jobs(chosen.key) if chosen else []
        if saved:
            st.divider()
            pick = st.selectbox("Or continue a saved job", saved, index=None, key=k + "saved", placeholder="Choose…",
                                format_func=lambda j: f"{j.d['created'].replace('T', ' ')} · {j.phase} · {j.d['ws']['branch']}")
            if st.button("Continue this job", key=k + "open", use_container_width=True, disabled=pick is None):
                st.session_state[k + "job"] = pick.id
                st.rerun()

    if go:
        try:
            job = flow.new_job(chosen, agent, source, base, model, None if use_em2 else sorted(picked),
                               step_by_step=how == "Step by step")
        except ws.GitError as exc:
            st.error(str(exc))
        else:
            st.session_state[k + "job"] = job.id
            if job.workspace.warning:
                st.warning(job.workspace.warning)
            if job.steps:
                st.rerun()                                    # the first manager's subtasks are the steps: nothing to plan
            _run("Engineering Manager 2 is exploring the repository…", flow.plan_turn, job)

    _board(tasks, k)
    job = flow.load(st.session_state.get(k + "job") or "")
    if not job:
        st.info("Pick a task and a repository, then press **Start a new job**.\n\n" + WHAT)
        if chosen:
            with st.expander("What the agent will be told"):
                st.text(task_list.briefing(chosen))
        return None
    _stage_strip(job.phase)
    if job.phase in ("planning", "jira"):
        _job_header(job)
        _memory_panel(k + job.id + "_", job)
        _gate_or(k + job.id + "_", job, {"planning": _planning, "jira": _jira}[job.phase])
    else:
        _frozen_summary(k + job.id + "_", job)
    return job


BOARD_ICON = {"not_started": "⚪", "planning": "🧭", "ready": "🟢", "in_progress": "🛠️", "blocked": "⛔", "push": "📤", "pr_open": "🔀", "merged": "🎉"}


def _open(k: str, row: board.Row) -> None:
    """Button callback: make this task the chosen one, and open its job if it has one."""
    st.session_state[k + "task"] = row.task
    st.session_state[k + "job"] = row.job.id if row.job else None


def _board(tasks: List[task_list.CodingTask], k: str) -> None:
    """Every task with its state; open one to see its subtasks, then open or start its job."""
    rows = board.rows(tasks)
    if not rows:
        return
    count = lambda *codes: sum(1 for r in rows if r.code in codes)
    st.markdown("**📋 Tasks** — " + f"{count('merged')} merged · {count('pr_open')} PR open · {count('ready', 'in_progress', 'push')} in progress · "
                f"{count('blocked')} blocked · {count('planning')} planning · {count('not_started')} not started")
    for r in rows:
        t = r.task
        with st.expander(f"{BOARD_ICON[r.code]} {t.jira_key + ' · ' if t.jira_key else ''}{t.story_id}/T{t.index} · {t.title} — {r.label}"):
            st.caption(f"Expert suggested by the first manager: `{t.agent}`" + (f" · [pull request]({r.pr_url})" if r.pr_url else ""))
            if r.job and r.job.steps:
                for s in r.job.steps:
                    st.markdown(f"{ICON[s['status']]} {s['n']}. {s['title']} — `{s.get('agent', '')}`" + (f" · `{s['commit'][:8]}`" if s.get("commit") else ""))
            else:
                for i, sub in enumerate(t.subtasks, start=1):
                    st.markdown(f"⚪ {i}. {sub['title']}")
            st.button("Open its job" if r.job else "Start this task", key=k + "board_" + t.key, on_click=_open, args=(k, r))


def _hero(job: flow.Job) -> None:
    """The job at a glance: the task, how the agent is working, branch and base, and one segment per subtask."""
    w = job.workspace
    done = sum(1 for x in job.steps if x["status"] in ("approved", "skipped"))
    current = job.current() if job.phase == "executing" else None
    segs = "".join(
        f"<span class='sd-seg {'done' if x['status'] == 'approved' else 'skip' if x['status'] == 'skipped' else 'now' if current is not None and x['n'] == current['n'] else ''}'"
        f" title='{_html.escape(x['title'], quote=True)}'></span>" for x in job.steps)
    mode = "🪜 Step by step" if flow.steps_mode(job) else "⚡ Autonomous"
    sub = f"{done} of {len(job.steps)} subtasks done" if job.steps else "No subtasks yet"
    st.markdown(
        f"<div class='sd-hero'><div class='sd-hero-top'><span class='sd-hero-title'>{_html.escape(job.task.label)}</span>"
        f"<span class='sd-mode'>{mode}</span></div>"
        f"<div class='sd-hero-meta'><span class='sd-tag'>🧑‍💻 {_html.escape(job.d['agent'])}</span><span class='sd-tag'>🌿 {_html.escape(w.branch)}</span>"
        f"<span class='sd-tag'>⤴ {_html.escape(w.base)}</span></div>"
        f"<div class='sd-segs'>{segs}</div><div class='sd-hero-sub'>{sub}</div></div>", unsafe_allow_html=True)


def _job_header(job: flow.Job) -> None:
    w = job.workspace
    _hero(job)
    st.caption(f"Default expert `{job.d['agent']}` · branch `{w.branch}` (from `{w.base}`) · worktree `{w.tree}`")
    with st.expander(f"What the agent did ({len(job.d['log'])} steps)"):
        st.write("\n".join(f"- {line}" for line in job.d["log"][-80:]) or "—")


def _memory_panel(k: str, job: flow.Job) -> None:
    """What Engineering Manager 2 remembers about this repository: readable, and you can clear it."""
    key = memory.key_for(job.workspace)
    found = memory.facts(key, job.workspace)
    with st.expander(f"🧠 What Engineering Manager 2 remembers about this repository ({len(found)})"):
        if not found:
            st.caption("Nothing yet. When you freeze the subtasks, what the manager learned is saved here and used by the next job on this repository.")
            return
        for f in found:
            st.markdown(("⚠️ " if f["stale"] else "• ") + f["text"] + (f"  \n<small>{', '.join(f['files'][:4])} · learned {f['learned']}"
                        + (" · files changed since: it will be re-checked" if f["stale"] else "") + "</small>" if f["files"] else ""),
                        unsafe_allow_html=True)
        if st.button("Forget everything about this repository", key=k + "forget"):
            memory.forget(key)
            st.rerun()


def _frozen_summary(k: str, job: flow.Job) -> None:
    """Engineering Manager 2 is finished: the frozen subtasks, folded away."""
    jira = job.d.get("jira") or {}
    st.success(f"Engineering Manager 2 is done: {len(job.steps)} subtask(s) frozen." + (f" {jira['note']}" if jira.get("status") == "updated" else ""))
    if job.d.get("memory_added"):
        st.caption(f"🧠 {job.d['memory_added']} new thing(s) learned about this repository were saved for the next job.")
    with st.expander("The frozen subtasks"):
        for s in job.steps:
            _step_card(s, full=True)
    if jira.get("status") in ("skipped", "unavailable"):
        st.caption("Jira not updated: " + jira["note"])
    _undo("Undo the freeze (reopen the subtasks)", k + "undo_freeze", flow.unfreeze_blocker(job), flow.unfreeze_plan, "", job)
    if jira.get("status") == "updated":
        _undo("Undo the Jira update", k + "undo_jira", "Jira keeps its own history; change the ticket there.", lambda: None)


STATUS_LABEL = {"pending": "Not started", "running": "Agent working", "review": "Code waiting for your approval",
                "testreview": "Tests waiting for your approval", "results": "Test report waiting for your decision",
                "accepted": "Ready to commit", "asking": "Waiting for your answer", "blocked": "Blocked", "approved": "Committed",
                "skipped": "Skipped"}


def _step_log(job: flow.Job, n: int) -> list:
    return [line for line in job.d["log"] if f"[{n}] " in line]


def _records(k: str, job: flow.Job) -> None:
    """A read-only look at any subtask, finished or not: what it did, its code changes, its unit tests and report, and its activity."""
    started = [s for s in job.steps if s["status"] != "pending"]
    if not started:
        return
    st.markdown(f"#### 🗂️ Subtask records · {len(started)} of {len(job.steps)} started")
    with st.container(border=True):
        numbers = [s["n"] for s in job.steps]
        pick = st.selectbox("Choose a subtask", numbers, index=numbers.index(started[-1]["n"]), key=k + "recpick",
                            format_func=lambda n: f"{ICON[job.step(n)['status']]}  {n}. {job.step(n)['title']} — {STATUS_LABEL[job.step(n)['status']]}")
        step = job.step(pick)
        n = step["n"]
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Status**  \n{ICON[step['status']]} {STATUS_LABEL[step['status']]}")
        c2.markdown(f"**Expert**  \n`{step.get('agent') or job.d['agent']}`")
        c3.markdown("**Commit**  \n" + (f"`{step['commit'][:8]}`" if step.get("commit") else "—"))
        if step["status"] == "pending":
            st.info("This subtask has not been started yet.")
            return
        what = {"approved": f"Undo the commit of step {n}", "skipped": f"Undo skipping step {n}"}.get(step["status"], f"Undo the changes of step {n}")
        _undo(what, k + f"undo_step{n}", flow.undo_step_blocker(job, n), flow.undo_step, "", job, n)
        t_sum, t_code, t_tests, t_log = st.tabs(["Summary", "Code changes", "Unit tests", "Activity"])
        with t_sum:
            st.write(step["summary"] or "No summary yet.")
            if step["assumptions"]:
                st.caption("Assumptions: " + "; ".join(step["assumptions"]))
            if step.get("message"):
                st.caption(f"Commit message: {step['message']}")
            if step.get("jira_note"):
                (st.success if step.get("jira_posted") else st.warning)("🗂️ " + step["jira_note"])
            st.markdown("**What the agent was asked to do**")
            _step_card(step, full=True)
        with t_code:
            if step.get("commit"):
                text = ws.commit_diff(job.workspace, step["commit"])
            elif step.get("start_sha") and step["status"] not in ("skipped", "pending"):
                text = ws.diff(job.workspace, step["start_sha"])
            else:
                text = ""
            show_diff(text, k + f"rec{n}")
        with t_tests:
            if step.get("test_report") is None and not step.get("tests_written") and not step.get("agent_runs"):
                st.info("No unit tests have been written or run for this subtask yet.")
            _tests_panel(k + "rec", job, step, readonly=True)
        with t_log:
            lines = _step_log(job, n)
            st.code("\n".join(lines) if lines else "No activity recorded for this subtask.", language="text")
            cmds = step.get("agent_commands") or []
            if cmds:
                st.markdown("**Shell commands the agent ran**")
                st.dataframe([{"time": c["when"], "command": c["command"], "result": "ok" if c["ok"] else "failed"} for c in cmds],
                             use_container_width=True, hide_index=True)


def _redo_panel(k: str, job: flow.Job) -> None:
    """Redo finished work from a chosen step (rewriting the agent's own branch), or revamp the whole job as a new one."""
    started = [s for s in job.steps if s.get("start_sha")]
    st.divider()
    with st.expander("♻️ Redo or revamp this job"):
        for h in job.d.get("history") or []:
            if h.get("revamped_as"):
                st.caption(f"{h['at'].replace('T', ' ')} · revamped as a new job `{h['revamped_as']}`")
            else:
                st.caption(f"{h['at'].replace('T', ' ')} · redone from step {h['from_step']} · {len(h['commits'])} commit(s) kept on branch `{h['backup']}`"
                           + (" · had been pushed" if h.get("pushed") else ""))
        pushed = (job.d.get("push") or {}).get("done")
        merged = job.d.get("merged")
        st.markdown("**Redo from a step** — takes the agent's branch back to just before that step; the step and every later one are done again.")
        if merged:
            st.info("The pull request is merged, so its commits cannot be rewritten. Use **Revamp as a new job** below.")
        elif not started:
            st.caption("No step has been started yet.")
        else:
            pick = st.selectbox("Redo from step", [s["n"] for s in started], key=k + "redo_from",
                                format_func=lambda n: f"{n}. {job.step(n)['title']} — {STATUS_LABEL[job.step(n)['status']]}")
            note = st.text_area("What should be different this time?", key=k + "redo_note", height=80)
            later = [s for s in job.steps if s["n"] >= pick and s["status"] in ("approved", "accepted")]
            st.warning(f"This removes the work of step {pick} and every later step from the agent's branch" + (f", including {len(later)} commit(s)" if later else "")
                       + ". The old tip is kept on a backup branch first." + (" The branch was already pushed: the remote is not touched now, and "
                       "pushing it again will ask you to confirm replacing it." if pushed else ""))
            if st.checkbox("I understand: redo from this step", key=k + "redo_ok") and st.button("Redo from this step", key=k + "redo_go", type="primary"):
                try:
                    flow.redo_from(job, pick, note.strip())
                except (ValueError, ws.GitError) as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
        st.markdown("**Revamp as a new job** — a new branch from the base as it is now, with this job's plan; this job's record stays as it is.")
        rnote = st.text_area("What should be different in the new job?", key=k + "revamp_note", height=80)
        if st.button("Start a new job from this plan", key=k + "revamp_go"):
            try:
                new = flow.revamp_job(job, rnote.strip())
            except (ValueError, ws.GitError) as exc:
                st.error(str(exc))
            else:
                st.session_state[k.rsplit(job.id, 1)[0] + "job"] = new.id
                st.rerun()


def _coding_section(k: str, job: flow.Job) -> None:
    _stage_strip(job.phase)
    _job_header(job)
    _records(k, job)
    _gate_or(k, job, {"executing": _executing, "final": _final, "done": _done}[job.phase])
    _redo_panel(k, job)


def _start_over(k: str, job: flow.Job) -> None:
    """A way out of a stale question or error: plan again (before freezing) or code the asking step again."""
    if job.phase == "planning":
        if st.button("Start over: plan again from scratch", key=k + "replan"):
            _run("Engineering Manager 2 is exploring the repository…", flow.replan, job)
    elif job.phase == "executing":
        step = job.current()
        if step is not None and step.get("start_sha") and step["status"] in ("asking", "blocked", "running", "testreview", "results", "review", "pending"):
            if st.button(f"Start over: code step {step['n']} again", key=k + "restart"):
                _run("Agent working…", flow.restart_step, job, step["n"])


def _gate_or(k: str, job: flow.Job, draw) -> None:
    """An error or the agent's question comes first; otherwise draw the stage."""
    if job.d["error"]:
        st.error(job.d["error"])
        if st.button("Continue", key=k + "continue"):
            _run("Agent working…", flow.continue_after_error, job)
        _start_over(k, job)
        return

    q = job.d["question"]
    if q:
        _question(k, job, q)
        _start_over(k, job)
        return
    draw(k, job)


def _question(k: str, job: flow.Job, q: dict) -> None:
    st.warning("The agent needs your input" + ("" if q["for"] == "plan" else f" on step {q['for']}") + ".")
    if q.get("summary"):
        st.write(q["summary"])
    st.markdown(f"**{q['question']}**")
    options = q["options"]
    pick = st.radio("Options", options, key=k + "pick",
                    index=options.index(q["recommended_option"]) if q["recommended_option"] in options else 0,
                    format_func=lambda o: o + ("  (recommended)" if o == q["recommended_option"] else "")) if options else None
    typed = st.text_input("Or answer in your own words", key=k + "typed")
    if st.button("Send answer", type="primary", key=k + "send", disabled=not (typed.strip() or pick)):
        _run("Agent working…", flow.answer_question, job, typed.strip() or pick)


# ---------- 1. the plan ----------

def _step_card(s: dict, full: bool = False) -> None:
    agent = f" · `{s['agent']}`" if s.get("agent") else ""
    st.markdown(f"**{s['n']}. {s['title']}**{agent}")
    if s["detail"]:
        st.write(s["detail"])
    if s.get("changes"):
        st.dataframe([{"file": c["path"], "action": c["action"], "what": c["what"]} for c in s["changes"]],
                     use_container_width=True, hide_index=True)
    elif s["files"]:
        st.caption("Files: " + ", ".join(f"`{f}`" for f in s["files"]))
    if full and s.get("context"):
        with st.expander("Context from the repository (so the coding agent does not have to read it)"):
            st.text(s["context"])
    if s.get("tests"):
        st.caption("Tests: " + s["tests"])
    if s["check"]:
        st.caption("Check: " + s["check"])


def _planning(k: str, job: flow.Job) -> None:
    st.markdown("### 1 · Engineering Manager 2: the subtasks")
    if job.d["plan"]["summary"]:
        st.write(job.d["plan"]["summary"])
    if job.d["plan"].get("repo_notes"):
        st.caption("Repository notes: " + job.d["plan"]["repo_notes"])
    if not job.steps:
        st.info("The manager has not produced the subtasks yet.")
        if st.button("Ask for the subtasks", key=k + "askplan"):
            _run("Engineering Manager 2 is exploring the repository…", flow.plan_turn, job)
        return
    st.caption("Look these over. Change the expert of any subtask, tell the manager what to reconsider, or freeze them when you are happy.")
    for s in job.steps:
        with st.container(border=True):
            _step_card(s, full=True)
            st.selectbox("Expert for this subtask", AGENTS, key=k + f"agent{job.d['plan_runs']}_{s['n']}",
                         index=AGENTS.index(s["agent"]) if s.get("agent") in AGENTS else 0)
    note = st.text_area("Want it different? Tell the manager what to reconsider", key=k + f"reconsider{job.d['plan_runs']}", height=80,
                        placeholder="e.g. split step 2, put the tests with each step, do not touch the payment module…")
    cmd = st.text_input("Command that runs the unit tests (used after every subtask)", value=job.d.get("test_command", ""),
                        key=k + f"testcmd{job.d['plan_runs']}",
                        help="One command, no pipes. Engineering Manager 2 proposes it from the build files; change it if it is wrong. "
                             "Without one, the tests are written but not run.")
    ok, why = testrun.validate_command(cmd) if cmd.strip() else (True, "")
    if cmd.strip() and not ok:
        st.error(why)
    elif not cmd.strip():
        st.warning("No test command: the coder will write the unit tests but they will not be run.")
    _start_over(k, job)
    _undo("Undo the plan (drop these subtasks)", k + "undo_plan", "", flow.discard_plan, "", job)
    c1, c2 = st.columns(2)
    if c1.button("Reconsider the subtasks", key=k + "recon", disabled=not note.strip(), use_container_width=True):
        _run("Engineering Manager 2 is revising the subtasks…", flow.plan_turn, job, user_input=note.strip())
    if c2.button("Freeze the subtasks", type="primary", key=k + "freeze", use_container_width=True, disabled=bool(cmd.strip()) and not ok):
        flow.set_agents(job, {s["n"]: st.session_state.get(k + f"agent{job.d['plan_runs']}_{s['n']}", s["agent"]) for s in job.steps})
        if cmd.strip() != job.d.get("test_command", "").strip():
            flow.set_test_command(job, cmd)
        flow.freeze_plan(job)
        st.rerun()


# ---------- 1b. may Jira be updated? ----------

def _jira(k: str, job: flow.Job) -> None:
    from coding import jira_sync
    st.markdown("### 1b · Update Jira?")
    st.success("The subtasks are frozen.")
    key = job.task.jira_key
    st.write(f"May I write them into the Jira ticket **{key}**? Its \"Steps\" list is replaced by the subtasks below, each with its expert "
             "agent, and the ticket gets a label per expert. Nothing else in the ticket is changed.")
    st.code(jira_sync.steps_markup(job.steps), language="text")
    info = job.d.get("jira") or {}
    if info.get("status") == "failed":
        st.error(info["note"])
    c1, c2 = st.columns(2)
    if c1.button("Yes, update Jira", type="primary", key=k + "jira_yes", use_container_width=True):
        with st.spinner("Updating Jira…"):
            flow.update_jira(job)
        st.rerun()
    if c2.button("No, leave Jira as it is", key=k + "jira_no", use_container_width=True):
        flow.skip_jira(job)
        st.rerun()
    _undo("Undo the freeze (reopen the subtasks)", k + "jira_reopen", flow.unfreeze_blocker(job), flow.unfreeze_plan, "", job)


# ---------- 2. the steps ----------

def _executing(k: str, job: flow.Job) -> None:
    st.markdown("### 2 · Carry out the subtasks")
    jira = job.d.get("jira") or {}
    if jira.get("status") == "updated":
        st.caption("✅ " + jira["note"])
    elif jira.get("status") in ("skipped", "unavailable"):
        st.caption("Jira not updated: " + jira["note"])
    step = job.current()
    _step_rows(job, step)
    if step is None:
        return
    if all(s["status"] == "pending" for s in job.steps) and jira.get("status") != "updated" and st.button("Reopen the subtasks", key=k + "reopen"):
        flow.unfreeze_plan(job)
        st.rerun()
    st.divider()
    _pipeline(step)
    with st.container(border=True):
        _step_card(step)
    n = step["n"]
    if step["status"] == "pending":
        if st.button(f"Execute step {n}", type="primary", key=k + f"exec{n}"):
            _run(f"Agent is carrying out step {n}…", flow.execute_step, job, n)
        return
    if step["status"] == "blocked":
        _blocked(k, job, step)
        return
    if step["status"] == "testreview":
        _test_review(k, job, step)
        return
    if step["status"] == "results":
        _results(k, job, step)
        return
    # review / accepted: the code is made and waits for you (then the tests, then the commit)
    gated = flow.steps_mode(job)
    if step["status"] == "accepted":
        st.success(f"Step {n} is done and its test report is in. Now the commit.")
    elif gated:
        st.info(f"Step {n}: the code is written and shown below. **No unit test has been written or run yet.** Approve the code and the agent "
                "writes the unit tests for you to review, or tell it what to change.")
    else:
        st.success(f"Step {n} is finished. The agent wrote the code and its unit tests, ran them and fixed what it could. "
                   "Review the result below: the code changes, then the unit tests and how many passed and failed.")
    st.write(step["summary"])
    if step["assumptions"]:
        st.caption("Assumptions: " + "; ".join(step["assumptions"]))
    if step["status"] == "accepted" or not gated:
        _tests_panel(k, job, step)
    show_diff(ws.diff(job.workspace, step["start_sha"]), k + f"d{n}")
    feedback = st.text_area("Not right? Tell the agent what to change in the code", key=k + f"fb{n}", height=80)
    if step["status"] == "review":
        c1, c2, c3 = st.columns(3)
        if gated:
            if c1.button("Approve the code and write the tests", type="primary", key=k + f"ok{n}", use_container_width=True):
                _run(f"Agent is writing the tests of step {n}…", flow.approve_code, job, n)
        elif c1.button("Approve the changes", type="primary", key=k + f"ok{n}", use_container_width=True):
            flow.accept_step(job, n)
            st.rerun()
    else:
        st.markdown("**Commit** — this saves the change on the agent's branch on this computer. Nothing is pushed yet. "
                    "If you do not commit, the step simply stays here until you do.")
        message = st.text_input("Commit message", key=k + f"msg{n}", value=step["message"] or step["title"])
        c1, c2, c3 = st.columns(3)
        from coding import jira_sync
        can, why = jira_sync.availability(job.task)
        post = st.checkbox(f"Also post this step's summary and unit-test report to Jira ({job.task.jira_key})" if can else "Post to Jira",
                           value=can, disabled=not can, key=k + f"post{n}", help=None if can else why)
        if not can:
            st.caption("Jira will not be told about this step: " + why)
        if c1.button("Commit this step", type="primary", key=k + f"commit{n}", use_container_width=True):
            flow.approve_step(job, n, message, post_to_jira=post)
            st.rerun()
    if c2.button("Reconsider with my input", key=k + f"redo{n}", disabled=not feedback.strip(), use_container_width=True):
        _run(f"Agent is redoing step {n}…", flow.reconsider, job, n, feedback.strip())
    if c3.button("Skip this step", key=k + f"skip{n}", use_container_width=True):
        flow.skip_step(job, n)
        st.rerun()


def _results(k: str, job: flow.Job, step: dict) -> None:
    """The unit tests ran: which passed, which failed and why. Failures: let the agent fix the code (with your input if you like) and
    run again, up to the limit per request; or go on with the report as it is. Then the commit decision."""
    n = step["n"]
    r = step.get("test_report") or {}
    st.markdown(f"### 📊 Step {n}: unit test report")
    _tests_panel(k, job, step)
    failing = testrun.needs_fix(r) if r.get("ran") else False
    with st.expander("The whole change (code and tests)"):
        show_diff(ws.diff(job.workspace, step["start_sha"]), k + f"rd{n}")
    if failing:
        st.warning(f"Some tests did not pass. The agent can change the code to fix them and run the tests again, for up to "
                   f"{flow.MAX_FIX_ATTEMPTS} rounds each time you allow it. Or keep the report as it is.")
        hint = st.text_area("Your input for the fix (optional: without it the agent works from what it found)", key=k + f"fixfb{n}", height=80,
                            placeholder="e.g. the expiry must be computed from the redeem time, not the creation time…")
        c1, c2 = st.columns(2)
        if c1.button(f"Let the agent fix the code and run again (up to {flow.MAX_FIX_ATTEMPTS} rounds)", type="primary", key=k + f"fix{n}",
                     use_container_width=True):
            _run(f"Agent is fixing step {n} and running the tests again…", flow.fix_and_rerun, job, n, hint.strip())
        if c2.button("Keep the report as it is and go to the commit", key=k + f"keep{n}", use_container_width=True):
            flow.accept_results(job, n)
            st.rerun()
    else:
        label = "Continue to the commit" if r.get("ran") else "Continue to the commit without a test run"
        if st.button(label, type="primary", key=k + f"cont{n}"):
            flow.accept_results(job, n)
            st.rerun()


def _test_files(job: flow.Job, step: dict) -> None:
    """The unit tests themselves: every test file of this subtask, with its code, to read before or after anything is run."""
    tree = job.workspace.tree.resolve()
    paths = flow.test_file_paths(job, step)
    st.markdown(f"**📄 Unit test code** ({len(paths)} file{'s' if len(paths) != 1 else ''})")
    if not paths:
        st.caption("No unit-test file was found among this subtask's changes.")
        return
    said = {t["file"]: t.get("description", "") for t in step.get("tests_written") or []}
    for rel in paths:
        path = (tree / rel).resolve()
        with st.expander(f"`{rel}`" + (f" — {said[rel]}" if said.get(rel) else "")):
            if tree not in path.parents or not path.is_file():
                st.caption("This file cannot be shown (it may have been removed since).")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            st.code(text[:20000] + ("\n… (cut)" if len(text) > 20000 else ""), language=path.suffix.lstrip(".") or "text", line_numbers=True)


def _test_review(k: str, job: flow.Job, step: dict) -> None:
    """Stage between writing and running: read the tests, ask for different ones, then approve them to run."""
    n = step["n"]
    st.info(f"Step {n}: you approved the code and the agent wrote its unit tests. **The tests have not been run yet.** Read them, ask for "
            "changes if you want different cases, and allow the run when you are satisfied.")
    st.write(step["summary"])
    if step["assumptions"]:
        st.caption("Assumptions: " + "; ".join(step["assumptions"]))
    if step.get("tests_written"):
        _test_files(job, step)
    else:
        st.warning("The coder did not list any test files. Ask it to write them, or check the diff below.")
    _agent_runs(step)
    with st.expander("The whole change (diff)"):
        show_diff(ws.diff(job.workspace, step["start_sha"]), k + f"td{n}")
    feedback = st.text_area("Different test cases? Tell the agent what to change in the tests", key=k + f"tfb{n}", height=80,
                            placeholder="e.g. add a test for an expired coupon, remove the duplicate success case, use realistic data…")
    cmd = st.text_input("Command that runs the unit tests", value=flow.test_command_for(job), key=k + f"tcmd{n}",
                        help="One command, no pipes. Without one the tests cannot be run.")
    ok, why = testrun.validate_command(cmd) if cmd.strip() else (False, "No test command is set.")
    if not ok:
        st.warning(why)
    c1, c2, c3 = st.columns(3)
    if c1.button("Reconsider the tests", key=k + f"retests{n}", disabled=not feedback.strip(), use_container_width=True):
        _run(f"Agent is changing the tests of step {n}…", flow.revise_tests, job, n, feedback.strip())
    if c2.button("Approve the tests and run them", type="primary", key=k + f"runtests{n}", disabled=not ok, use_container_width=True):
        if cmd.strip() != flow.test_command_for(job).strip() or not job.d.get("test_command"):
            flow.set_test_command(job, cmd)
        _run(f"Running the tests for step {n}…", flow.approve_tests, job, n)
    if c3.button("Approve without running", key=k + f"notests{n}", use_container_width=True):
        flow.skip_running_tests(job, n)
        st.rerun()


def _latest_agent_run(step: dict) -> Optional[dict]:
    return testrun.latest_run(step.get("agent_runs"))


def _agent_headline(step: dict) -> None:
    """Totals and the pass/fail bar of the agent's last test run: what it saw when it finished."""
    last = _latest_agent_run(step)
    if last is None:
        st.warning("The agent's test runs gave no results that could be read (see the runs below).")
        return
    st.markdown(f"**Result of the agent's last test run** ({last['when']}, {last.get('source') or 'output'})")
    _result_bar(last)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total tests", last["total"])
    c2.metric("Passed", last["passed"])
    c3.metric("Failed", last["failed"])
    c4.metric("Skipped", last["skipped"])


STATUS_WORD = {"passed": "✅ passed", "failed": "❌ failed", "build_failed": "⛔ build failed", "unknown": "❔ no results"}


def _agent_runs(step: dict) -> None:
    """Every test run the agent made, as it ran it: when, which command, its status, each test, and why the failures failed."""
    runs = step.get("agent_runs") or []
    if not runs:
        return
    st.markdown("**🤖 Test runs by the agent** (newest first)")
    st.dataframe([{"time": r["when"], "during": {"code": "code", "cases": "code", "tests": "writing tests", "fix": "fixing"}.get(r.get("phase"), r.get("phase", "")),
                   "command": r["command"], "status": STATUS_WORD.get(r.get("status"), "❔"), "result": testrun.run_headline(r)}
                  for r in reversed(runs)], use_container_width=True, hide_index=True)
    for i, r in reversed(list(enumerate(runs, start=1))[-8:]):
        with st.expander(f"Run {i} · {r['when']} · {STATUS_WORD.get(r.get('status'), '')} · {testrun.run_headline(r)}"):
            st.caption(f"`{r['command']}`" + (f" · results read from the {r['source']}" if r.get("source") else ""))
            failing = [c for c in r.get("cases", []) if c.get("status") == "failed"]
            if r.get("status") == "build_failed" and r.get("reason"):
                st.error("Why it did not run: " + r["reason"])
            for c in failing:
                st.error(f"`{c['name']}` — " + (c.get("message") or "it failed; no message was recorded"))
            if r.get("cases"):
                st.dataframe([{"test": c["name"], "result": c["status"], "why": c.get("message", "")} for c in r["cases"]],
                             use_container_width=True, hide_index=True)
            st.code(r.get("tail") or "(no output)", language="text")


def _tests_panel(k: str, job: flow.Job, step: dict, readonly: bool = False) -> None:
    """The unit-test results of a subtask. When the agent ran tests itself, its runs lead: that is what it saw. The program's own
    re-run follows as a second opinion."""
    if step.get("agent_runs"):
        st.markdown("**🧪 Unit tests**")
        _agent_headline(step)
        _agent_runs(step)
        _test_files(job, step)
        if step.get("test_report") is not None:
            st.markdown("**Independent re-run by the program**")
            _tests_report(k, job, step, readonly, metrics=False)
    else:
        _tests_report(k, job, step, readonly)
        _test_files(job, step)


def _tests_report(k: str, job: flow.Job, step: dict, readonly: bool = False, metrics: bool = True) -> None:
    """The unit-test result for a subtask: totals, what was fixed on the way, and what could not be fixed and why."""
    r = step.get("test_report")
    if r is None:
        return
    if metrics:
        st.markdown("**🧪 Unit tests**")
    if not r.get("ran"):
        st.warning("The tests did not run. " + (r.get("note") or ""))
        if readonly:
            if r.get("command"):
                st.caption(f"Command: `{r['command']}`")
            return
        n = step["n"]
        cmd = st.text_input("Command that runs the unit tests", value=r.get("command") or flow.test_command_for(job), key=k + f"pcmd{n}",
                            help="One command, no pipes, for example ./mvnw test or python3 -m pytest -q.")
        ok, why = testrun.validate_command(cmd) if cmd.strip() else (False, "Type the command that runs this repository's unit tests.")
        if not ok:
            st.caption(why)
        if st.button("Run the tests", key=k + f"retest{n}", disabled=not ok):
            if cmd.strip() != job.d.get("test_command", "").strip():
                flow.set_test_command(job, cmd)
            _run(f"Running the tests for step {n}…", flow.rerun_tests, job, n)
        return
    if r.get("build_failed"):
        st.error("The build failed before any test could run.")
    if metrics:
        _result_bar(r)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total tests", r["total"])
        c2.metric("Passed", r["passed"])
        c3.metric("Failed", r["failed"])
        c4.metric("Skipped", r["skipped"])
    else:
        st.caption(f"{testrun.headline(r)} — the program ran the tests itself once more, in its own environment; it can differ from the agent's.")
    runs = r.get("attempts") or []
    if len(runs) > 1:
        st.caption("Run history: " + " → ".join(f"run {a['run']}: " + ("build failed" if a["build_failed"] else f"{a['failed']} failed of {a['total']}") for a in runs)
                   + (" — fixed along the way." if r["failed"] == 0 and not r.get("build_failed") else ""))
    for u in r.get("unresolved") or []:
        st.error(f"Not fixed: `{u['test']}` — {u['reason']}")
    if step.get("tests_written"):
        with st.expander(f"New test files ({len(step['tests_written'])})"):
            for t in step["tests_written"]:
                st.markdown(f"- `{t['file']}`" + (f" — {t['description']}" if t["description"] else ""))
    names = [c for c in r.get("cases", []) if c["status"] != "failed"]
    if names:
        with st.expander(f"All {len(r['cases'])} test results"):
            st.dataframe([{"test": c["name"], "result": c["status"], "message": c["message"]} for c in r["cases"]],
                         use_container_width=True, hide_index=True)
    if r.get("build_failed") and r.get("tail"):
        with st.expander("Build output (last lines)"):
            st.code(r["tail"], language="text")
    st.caption(f"Command: `{r.get('command', '')}` · {r.get('seconds', 0)}s")


def _blocked(k: str, job: flow.Job, step: dict) -> None:
    b = job.d.get("blocked") or {}
    n = step["n"]
    st.error(f"⛔ Step {n} cannot be done yet.")
    st.write(b.get("reason", ""))
    if b.get("waiting_for"):
        st.caption("Waiting for: " + b["waiting_for"])
    key = job.task.jira_key
    reported = b.get("reported")
    if reported:
        (st.success if reported["ok"] else st.error)(reported["note"])
    elif key:
        st.caption(f"I can write this reason on Jira ticket {key} (in the field JIRA_BLOCKED_REASON_FIELD if it is set, otherwise as a comment).")
    c1, c2, c3 = st.columns(3)
    if key and not (reported and reported["ok"]) and c1.button("Write the reason to Jira", key=k + f"bl_jira{n}", use_container_width=True):
        flow.report_blocked(job)
        st.rerun()
    if c2.button("It is unblocked: try again", type="primary", key=k + f"bl_retry{n}", use_container_width=True):
        _run(f"Agent is retrying step {n}…", flow.retry_blocked, job, n)
    if c3.button("Skip this step", key=k + f"bl_skip{n}", use_container_width=True):
        flow.skip_step(job, n)
        st.rerun()


# ---------- 3. push, pull request, Jira ----------

def _final(k: str, job: flow.Job) -> None:
    st.markdown("### 3 · Push and pull request")
    summary = flow.final_summary(job)
    if not summary["commits"]:
        st.warning("Every step was skipped or empty, so there is nothing to push.")
        return
    st.markdown("**Commits**")
    for c in summary["commits"]:
        st.markdown(f"- `{c['sha'][:8]}` {c['subject']}")
    files = diffview.parse(summary["diff"])
    st.markdown("**Files changed**")
    st.dataframe([{"file": p, "change": status, "added": a, "removed": r} for p, status, a, r in diffview.summarize(files)],
                 use_container_width=True, hide_index=True)
    overview = flow.tests_overview(job)
    if overview:
        st.markdown("**🧪 Unit tests**")
        for r in overview:
            st.markdown(f"- Step {r['n']} · {r['title']}: **{r['headline']}**")
            for u in r["unresolved"]:
                st.error(f"Not fixed: `{u['test']}` — {u['reason']}")
            if r.get("rerun"):
                st.caption(r["rerun"])
    show_diff(summary["diff"], k + "final")
    st.divider()
    w = job.workspace
    push = job.d.get("push") or {}
    stale = bool(push.get("done") and push.get("stale"))
    if not push.get("done") or stale:
        st.subheader("Push" if not stale else "Push again (replaces the earlier commits)")
        if stale:
            st.warning(f"Branch `{w.branch}` was pushed before and has since been redone. Pushing it again REPLACES the earlier commits on "
                       f"`{push['remote']}`. It is refused if somebody else added commits to that branch meanwhile. An open pull request "
                       "updates itself to show the new commits.")
        squash = None
        if len(summary["commits"]) > 1 and st.checkbox("Squash the commits into one before pushing", key=k + "squash"):
            squash = st.text_input("Commit message", key=k + "squashmsg", value=flow.default_pr_title(job))
        st.caption(f"This pushes branch `{w.branch}` ({len(summary['commits'])} commit(s)) to the repository's origin. It is never forced, and nothing else is touched.")
        sure = (not stale) or st.checkbox("I understand: replace the earlier commits on the remote branch", key=k + "replace_ok")
        if st.button("Push the branch" if not stale else "Replace the remote branch", type="primary", key=k + "push", disabled=not sure):
            try:
                flow.push_branch(job, squash, replace_remote=stale)
            except ws.GitError as exc:
                st.error(str(exc))
            else:
                if stale and (job.d.get("pr") or {}).get("opened"):
                    flow.finish_after_repush(job)
                st.rerun()
        return
    st.success(f"Pushed {push['commits']} commit(s) to `{push['remote']}` on branch `{w.branch}`.")
    _undo("Undo the push (remove the branch from the remote)", k + "undo_push", flow.undo_push_blocker(job), flow.undo_push,
          f"I understand: this deletes branch `{w.branch}` from the remote. The commits stay here.", job)
    st.subheader("Pull request")
    title = st.text_input("Pull request title", key=k + "title", value=flow.default_pr_title(job))
    body = st.text_area("Description", key=k + "body", value=flow.pr_description(job), height=220)
    st.caption(f"This opens a pull request from `{w.branch}` into `{w.base}`.")
    if st.button("Open the pull request", type="primary", key=k + "pr"):
        flow.open_pr(job, title, body)
        st.rerun()


def _jira_gate(k: str, job: flow.Job, field: str, question: str, actions: List[str], yes: str, run, skip) -> None:
    info = job.d.get(field) or {}
    status = info.get("status")
    if status == "updated":
        st.success("🗂️ " + info["note"])
    elif status in ("skipped", "unavailable"):
        st.caption("Jira not updated: " + (info.get("note") or ""))
    elif status in ("pending", "failed"):
        if status == "failed":
            st.error(info["note"])
        st.markdown(f"**{question}**")
        for a in actions:
            st.markdown(f"- {a}")
        c1, c2 = st.columns(2)
        if c1.button(yes, type="primary", key=k + field + "_yes", use_container_width=True):
            with st.spinner("Updating Jira…"):
                run(job)
            st.rerun()
        if c2.button("No, leave Jira as it is", key=k + field + "_no", use_container_width=True):
            skip(job)
            st.rerun()


def _done(k: str, job: flow.Job) -> None:
    from coding import jira_sync
    pr = job.d["pr"] or {}
    (st.success if pr.get("opened") else st.info)(pr.get("note") or "Pull request opened.")
    if pr.get("url"):
        st.markdown(f"[{pr['url']}]({pr['url']})")
    if job.d.get("merged"):
        st.success("🎉 The pull request has been merged.")
    _undo("Undo the pull request (close it)", k + "undo_pr", flow.undo_pr_blocker(job), flow.undo_pr,
          "I understand: this closes the pull request on GitHub (it can be reopened there). The branch stays pushed.", job)
    if (job.d.get("jira_pr") or {}).get("status") == "updated":
        _undo("Undo the Jira update", k + "undo_jira_pr", "Jira keeps its own history; change the ticket there.", lambda: None)
    _jira_gate(k, job, "jira_pr", f"Update Jira ticket {job.task.jira_key}?",
               (jira_sync.planned_after_pr(job.task, pr.get("url", ""), with_tests=bool(flow.tests_text(job))) if pr.get("opened")
                else jira_sync.planned_after_branch(job.task, job.workspace.branch, with_tests=bool(flow.tests_text(job)))),
               "Yes, update Jira", flow.jira_report_pr, flow.skip_jira_pr)
    if not job.d.get("merged") and pr.get("url"):
        if st.session_state.get(k + "merge_checked") is None:         # once when the job is opened; the button asks again
            st.session_state[k + "merge_checked"] = flow.check_merged(job) or ""
            if job.d.get("merged"):
                st.rerun()
        if st.button("Check whether the pull request was merged", key=k + "merged"):
            state = flow.check_merged(job)
            st.session_state[k + "merge_checked"] = state or ""
            if job.d.get("merged"):
                st.rerun()
            st.info({"open": "Still open.", "closed": "It was closed without being merged."}.get(state, "I could not tell (set GITHUB_TOKEN so I can ask GitHub)."))
    if job.d.get("merged"):
        _jira_gate(k, job, "jira_done", f"The pull request is merged. Move Jira ticket {job.task.jira_key} to Done?",
                   [f"Move {job.task.jira_key} to its Done status."], "Yes, mark it Done", flow.jira_mark_done, flow.skip_jira_done)
