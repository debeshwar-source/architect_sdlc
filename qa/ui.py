"""The QA screen: after a pull request is merged, you choose which test cases QA checks, it checks them and reports with proof, and you decide
on each test case yourself: sign off a pass, override a failure, or send it to the coding agent. Nothing is signed off as a whole.
"""

import subprocess
from typing import Any, Dict, List

import streamlit as st
import streamlit.components.v1 as components

from coding import agent as coding_agent
from coding import diffview, testrun
from qa import fix as qa_fix, flow, plan as qa_plan
from review import github, repos

S = "qa_"
RESULT = {"pass": "✅ Pass", "fail": "❌ Fail", "inconclusive": "❔ Inconclusive"}
SEV = {"blocker": "🔴 blocker", "major": "🟠 major", "minor": "🟡 minor"}


def _working(label: str, fn, *args, **kwargs) -> bool:
    """Run one long step with a spinner and the agent's latest action; an error becomes a message. True on success."""
    line = st.empty()
    try:
        with st.spinner(label):
            fn(*args, on_event=lambda t: line.caption(f"…{t[:140]}"), **kwargs)
    except (flow.QAError, github.GithubError, coding_agent.Incomplete, ValueError) as exc:
        st.error(str(exc))
        return False
    return True


def _default_name() -> str:
    if S + "name" not in st.session_state:
        try:
            st.session_state[S + "name"] = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            st.session_state[S + "name"] = ""
    return st.session_state[S + "name"]


def _merged(repo: str, refresh: bool) -> List[Dict[str, Any]]:
    key = f"{S}merged:{repo.lower()}"
    if refresh or key not in st.session_state:
        owner, name = repo.split("/", 1)
        st.session_state[key] = github.list_merged_prs(owner, name)
    return st.session_state[key]


def render(default_model: str = "") -> None:
    if not github.token():
        st.warning("Set `GITHUB_TOKEN` in `.env`, then reload.")
        return
    repo_list = repos.known_repos()
    if not repo_list:
        st.info("No repository known yet. Add one in the Code reviewer.")
        return
    _, right = st.columns([5, 1])
    refresh = right.button("↻ Refresh", key=S + "refresh", use_container_width=True)
    found: List[Dict[str, Any]] = []
    for repo in repo_list:
        try:
            found += [{**p, "repo": repo} for p in _merged(repo, refresh)]
        except github.GithubError as exc:
            st.warning(f"**{repo}**: {exc}")
    if not found:
        st.info("No merged pull requests were found in " + ", ".join(repo_list) + ".")
        return
    multi = len(repo_list) > 1
    options = [(p["repo"].lower(), p["number"]) for p in found]
    by_key = dict(zip(options, found))

    def label(k) -> str:
        p = by_key[k]
        rec = flow.load(*p["repo"].split("/", 1), p["number"])
        mark = "" if not rec else "✅ " if rec["results"] and all(i in rec["signoffs"] for i in rec["results"]) else "🧪 "
        return f"{mark}{p['repo'] + ' · ' if multi else ''}#{p['number']}  {p['title']}  —  merged {p['merged_at'][:10]}"

    pr = by_key[st.selectbox(f"Merged pull requests ({len(found)})", options, format_func=label, key=S + "pick")]
    owner, name = pr["repo"].split("/", 1)
    st.markdown(f"[Open the pull request ↗]({pr['url']})")
    for line in st.session_state.pop(S + "flash", None) or []:
        (st.success if line.startswith("✅") else st.info)(line)

    state = flow.load(owner, name, pr["number"])
    if not state:
        _choose_plan(owner, name, pr)
        return
    _render_state(state, default_model)


def _choose_plan(owner: str, name: str, pr: Dict[str, Any]) -> None:
    """Which test plan this pull request is tested against: found from the coding job that opened it, or picked."""
    runs = qa_plan.runs_with_plans()
    if not runs:
        st.info("No saved run has a test plan.")
        return
    detected = qa_plan.find_run_for_pr(owner, name, pr["branch"])
    ids = [r["run_id"] for r in runs]
    fmt = lambda rid: next(f"{r['run_id'][:15]} · {r['title']} ({r['cases']} test cases)" for r in runs if r["run_id"] == rid)
    chosen = st.selectbox("Test plan", ids, index=ids.index(detected) if detected else 0, format_func=fmt, key=f"{S}plan_{owner}/{name}#{pr['number']}")
    st.caption("Matched from the coding job." if detected and chosen == detected else "Choose the test plan." if not detected else "")
    test_plan = qa_plan.load_plan(chosen)
    with st.expander(f"The plan: {len(test_plan['test_cases'])} test cases"):
        st.write(test_plan["summary"])
        st.dataframe([{"id": c["id"], "title": c["title"], "priority": c["priority"], "component": c["component"], "type": c["type"]}
                      for c in test_plan["test_cases"]], hide_index=True, use_container_width=True)
    if st.button("▶ Start QA for this pull request", type="primary", key=S + "start"):
        flow.start(owner, name, pr, test_plan)
        st.rerun()


def _render_state(state: Dict[str, Any], model: str) -> None:
    kp = f"{S}{state['number']}_"
    top = st.columns([5, 1])
    top[0].markdown(f"##### QA of #{state['number']} · {state['plan_title']} · {len(state['cases'])} test cases")
    if top[1].button("Start over", key=kp + "reset"):
        flow.discard(state)
        st.session_state[S + "flash"] = ["Started over: the QA record for this pull request was cleared."]
        st.rerun()
    if state.get("error"):
        st.error(state["error"])
    _cases(state, kp, model)
    if state["results"]:
        _summary(state, kp)


def _tick(state: Dict[str, Any], ids: List[str]) -> None:
    flow.set_selection(state, ids, redraw=True)


def _group_of(state: Dict[str, Any], i: str, a: Dict[str, Any]) -> str:
    if i in state["results"]:
        return "checked"
    if not a:
        return "unassessed"
    return {"can_verify": "can", "partial": "partly"}.get(a[i]["verdict"] if i in a else "", "cannot")


GROUPS = [("checked", "Checked", ""), ("can", "Can be checked", ""), ("partly", "Partly", "QA can check only part of these."),
          ("cannot", "Can't be checked here", "Run only if you want QA to try anyway."), ("unassessed", "Not assessed yet", "")]


def _cases(state: Dict[str, Any], kp: str, model: str) -> None:
    """Every test case in the plan, grouped so it is clear which are done, which QA can check, and which it cannot. Tick any and run QA on
    them, or run or redo one on its own."""
    a = (state.get("assessment") or {}).get("items") or {}
    epoch = state.get("tick_epoch", 0)
    cases = {c["id"]: c for c in state["cases"]}
    rank = lambda i: (qa_plan.PRIORITY.get(cases[i]["priority"], 9), list(cases).index(i))
    n1, n2 = st.columns(2)
    n1.text_input("Your name", value=_default_name(), key=kp + "name")
    guidance = n2.text_input("Note for QA (optional)", key=kp + "guide")
    left, mid, right = st.columns([1, 1, 4])
    left.button("Select all", key=kp + "all", on_click=_tick, args=(state, list(cases)))
    mid.button("Clear", key=kp + "none", on_click=_tick, args=(state, []))
    if not a and right.button("Find out which can be tested", type="primary", key=kp + "assess"):
        if _working("QA is reading the code…", flow.run_assess, state, model or None):
            st.rerun()
    picked: List[str] = []
    for key, title, note in GROUPS:
        ids = sorted((i for i in cases if _group_of(state, i, a) == key), key=rank)
        if not ids:
            continue
        st.markdown(f"#### {title} ({len(ids)})")
        if note:
            st.caption(note)
        for i in ids:
            c, r = cases[i], state["results"].get(i)
            row = st.columns([1, 9, 2])
            if row[0].checkbox("Select", value=i in state["selected"], key=f"{kp}sel{epoch or ''}_{i}", label_visibility="collapsed"):
                picked.append(i)
            row[1].markdown(f"**{i}** · {c['title']}  \n{c['priority']} · {c['component']}" + (f" · {RESULT[r['result']]}" if r else ""))
            if r:
                with row[1]:
                    _outcome(state, i, kp, model, guidance)
            if key == "cannot":
                row[1].caption(a[i]["blockers"] or ("Not part of this change." if a[i]["verdict"] == "out_of_scope" else "QA can't check this from here."))
            if row[2].button("↻ Redo" if r else "▶ Try" if key == "cannot" else "▶ Run", key=f"{kp}go_{i}"):
                if _working(f"QA is checking {i}…", flow.run_checks, state, model or None, only=[i], guidance=guidance):
                    st.session_state[S + "flash"] = [f"✅ Checked {i}."]
                    st.rerun()
    redo = sum(i in state["results"] for i in picked)
    if st.button(f"▶ Run QA on {len(picked)} selected" + (f" ({redo} redo)" if redo else ""), type="primary" if a else "secondary", key=kp + "run", disabled=not picked):
        flow.set_selection(state, picked)
        if _working(f"QA is checking {len(picked)} test case(s)…", flow.run_checks, state, model or None, only=picked, guidance=guidance):
            st.session_state[S + "flash"] = [f"✅ Checked {len(picked)} test case(s)."]
            st.rerun()


def _outcome(state: Dict[str, Any], i: str, kp: str, model: str, note: str) -> None:
    """What QA found for ONE test case, right under it, and the decision on that test case alone. A pass says why and can be signed off. A failure
    says why and offers two things: override it, or ask the coding agent to fix it."""
    r, d, f = state["results"][i], flow.decision_of(state, i), qa_fix.latest(state, i)
    fail = r["result"] == "fail"
    name = st.session_state.get(kp + "name", _default_name())
    st.markdown(("Passed: " if r["result"] == "pass" else "Failed: " if fail else "Not settled: ") + r["summary"])
    for x in r["issues"]:
        st.markdown(f"- {SEV[x['severity']]} {x['title']}" + (f" (`{x['where']}`)" if x["where"] else ""))
    if r["evidence"] or r["issues"] or r["commands_run"]:
        with st.expander("Evidence"):
            for x in r["issues"]:
                st.markdown(f"**{x['title']}**: {x['detail']}")
            for e in r["evidence"]:
                st.markdown(f"- {e}")
            for cmd in r["commands_run"]:
                st.code(cmd, language="text")
    if d:
        st.caption(f"✅ Signed off by {d['by']}" if d["decision"] == "signed_off" else f"⚖️ Overridden by {d['by']}: {d['note']}")
        if st.button("↶ Take back", key=f"{kp}unw_{i}"):
            flow.take_back(state, i)
            st.rerun()
    elif r["result"] == "pass":
        if f and f.get("verified"):
            st.caption("Fixed and verified" + (f" ([#{f['pr']['number']}]({f['pr']['url']}))" if f.get("pr") else ""))
        if st.button("✅ Sign off", type="primary", key=f"{kp}so_{i}", disabled=not name.strip()):
            flow.sign_off_case(state, i, name)
            st.rerun()
    else:
        flag = f"{S}ovr_{state['number']}_{i}"
        c1, c2, _ = st.columns([1, 1, 3])
        if c1.button("⚖️ Override", key=f"{kp}ovr_{i}"):
            st.session_state[flag] = not st.session_state.get(flag, False)
        if fail and f is None and c2.button("🛠️ Coding agent", key=f"{kp}fix_{i}"):
            if _working(f"The coding agent is working on {i}…", qa_fix.run_fix, state, i, note, model or None):
                st.rerun()
        if st.session_state.get(flag):
            reason = st.text_input("Why is it acceptable?", key=f"{kp}why_{i}")
            if st.button("Confirm override", type="primary", key=f"{kp}wv_{i}", disabled=not reason.strip() or not name.strip()):
                flow.override_case(state, i, name, reason)
                st.session_state.pop(flag, None)
                st.rerun()
    if fail and f is not None and not d:
        _fix_panel(state, i, kp, model)


REVIEW = {"not_reviewed": "⚪ not sent to the code reviewer yet", "in_review": "🟡 the code reviewer is working through it", "cleared": "🟢 the code reviewer cleared it",
          "merged": "🟣 merged"}


def _open_in_reviewer(owner: str, name: str, number: int) -> None:
    """Take the person to this pull request in the Code reviewer (the same review, fix and merge loop as any other)."""
    st.session_state["dd_review"] = True
    st.session_state["rv_pick"] = (f"{owner}/{name}".lower(), number)
    for key in [k for k in st.session_state if str(k).startswith("rv_prs:")]:
        st.session_state.pop(key, None)                  # the list of open pull requests must be read again: the fix is new
    st.rerun()


def _fix_panel(state: Dict[str, Any], i: str, kp: str, model: str) -> None:
    """The loop for ONE failed test case: the coding agent fixes it -> you approve -> the code reviewer clears the pull request -> QA tests again."""
    owner, name = state["repo"]
    f = qa_fix.latest(state, i)
    st.markdown("**🔁 Fix**")
    why = qa_fix.blocker(state, i)
    if why:
        st.caption(why)
        return

    def ask(label: str, hint: str = "") -> None:
        note = st.text_input("Note for the coding agent (optional)", key=f"{kp}fixnote_{i}")
        if st.button(label, type="primary", key=f"{kp}fix_{i}"):
            if _working(f"The coding agent is working on {i}…", qa_fix.run_fix, state, i, note, model or None):
                st.rerun()

    if f is None:
        ask("🛠️ Ask the coding agent to fix it")
        return
    if f["status"] in ("ready", "no_change"):
        _fix_gate(state, i, f, kp, model)
        return
    if f["status"] == "pushed":
        st.warning("The fix is pushed, but its pull request could not be opened.")
        if st.button("Open the pull request", type="primary", key=f"{kp}openpr_{i}"):
            try:
                qa_fix.open_pr(state, i)
            except flow.QAError as exc:
                st.error(str(exc))
                return
            st.rerun()
        return
    # the fix has a pull request: where it stands, and what comes next
    rs = qa_fix.review_state(state, i)
    st.markdown(f"Round {f['n']}: [fix pull request #{f['pr']['number']} ↗]({f['pr']['url']}) · {REVIEW.get(rs, '')}")
    c1, c2, c3 = st.columns(3)
    if f["status"] == "pr_open" and c1.button("🔍 Send to the code reviewer", key=f"{kp}torev_{i}"):
        _open_in_reviewer(owner, name, f["pr"]["number"])
    if c2.button("↻ Check where it stands", key=f"{kp}sync_{i}"):
        if _working("Asking GitHub…", lambda s_, t, on_event=None: qa_fix.sync(s_, t), state, i):
            st.rerun()
    block = qa_fix.retest_blocker(state, i)
    skip = bool(block) and "not cleared" in block and st.checkbox("Test without the code reviewer's clearance (at my own risk)", key=f"{kp}skiprev_{i}")
    if c3.button("🧪 Ask QA to test again", type="primary", key=f"{kp}retest_{i}", disabled=bool(block) and not skip):
        if _working(f"The QA agent is testing {i} again…", flow.run_checks, state, model or None, only=[i], skip_review=skip):
            st.rerun()
    if block and not skip:
        st.caption(block)
    if f["status"] == "closed":
        st.warning(f"Fix pull request #{f['pr']['number']} was closed unmerged.")
        ask("🛠️ Ask the coding agent to try again")
    elif qa_fix.retested(state, i):
        st.info("Still fails after the fix. " + ("Next change: same pull request." if f["status"] == "pr_open" else "Next change: new pull request."))
        ask("🛠️ Ask the coding agent to try again")


def _fix_gate(state: Dict[str, Any], i: str, f: Dict[str, Any], kp: str, model: str) -> None:
    """The coding agent's answer, for you to read before anything is committed, pushed or opened."""
    st.markdown(f"**Coding agent** (round {f['n']})")
    (st.info if f["status"] == "no_change" else st.success)(f["summary"] or "No summary.")
    if f["explanation"]:
        st.caption(f["explanation"])
    if f["status"] == "no_change":
        st.warning({"not_a_bug": "The coding agent thinks the code is right and changed nothing.", "cannot_fix": "The coding agent could not fix it."}.get(
            f["outcome"], "The coding agent changed no file."))
    else:
        tests = f.get("tests") or {}
        rep = tests.get("report")
        st.markdown("**Tests**")
        if rep:
            (st.success if rep.get("ran") and not (rep.get("failed") or rep.get("build_failed")) else st.error)(testrun.headline(rep))
            for c in rep.get("failed_cases") or []:
                st.markdown(f"- `{c['name']}`" + (f": {c['message']}" if c.get("message") else ""))
        else:
            st.warning(tests.get("note") or "The tests were not run.")
        cmd_col, run_col = st.columns([4, 1])
        cmd = cmd_col.text_input("Test command", value=tests.get("command") or "", key=f"{kp}fixcmd_{i}")
        run_col.write("")
        if run_col.button("↻ Run again", key=f"{kp}fixrun_{i}", disabled=not cmd.strip()):
            try:
                qa_fix.run_tests(state, i, cmd)
            except flow.QAError as exc:
                st.error(str(exc))
                return
            st.rerun()
        files = diffview.parse(qa_fix.diff(state, i))
        if files:
            components.html(diffview.render(files), height=diffview.height(files), scrolling=True)
    blocked = qa_fix.tests_block(f) if f["status"] == "ready" else ""
    override = bool(blocked) and st.checkbox("These failures are not caused by this change: let me approve anyway", key=f"{kp}fixovr_{i}")
    left, mid, right = st.columns(3)
    if f["status"] == "ready":
        msg = st.text_input("Commit message", value=f"Fix {i}: {flow.case_of(state, i)['title']}"[:100], key=f"{kp}fixmsg_{i}")
        if blocked and not override:
            st.caption("Approval is blocked until the tests pass.")
        if left.button("✅ Approve: commit, push and open the pull request" if not f.get("pr") else "✅ Approve: commit and push to the open pull request",
                       type="primary", key=f"{kp}fixok_{i}", disabled=bool(blocked) and not override):
            try:
                qa_fix.approve_fix(state, i, msg, allow_failing_tests=override)
            except flow.QAError as exc:
                st.error(str(exc))
                return
            st.session_state[S + "flash"] = [f"✅ Fix for {i} pushed" + (f"; pull request #{qa_fix.latest(state, i)['pr']['number']} opened." if qa_fix.latest(state, i).get("pr") else ".")]
            st.rerun()
    if mid.button("↶ Discard", key=f"{kp}fixdis_{i}"):
        try:
            qa_fix.discard_fix(state, i)
        except flow.QAError as exc:
            st.error(str(exc))
            return
        st.rerun()
    note = st.text_input("Or tell the coding agent what to do differently", key=f"{kp}fixagain_{i}")
    if right.button("↻ Ask again", key=f"{kp}fixask_{i}", disabled=not note.strip()):
        if _working(f"The coding agent is working on {i}…", qa_fix.run_fix, state, i, note, model or None):
            st.rerun()


def _summary(state: Dict[str, Any], kp: str) -> None:
    """One line on where things stand, and the record: put it on the pull request, or download it."""
    c = flow.counts(state)
    st.markdown(f"**{c['signed_off']} signed off · {c['overridden']} overridden · {c['waiting']} waiting · {c['not_checked']} not checked**")
    left, right, _ = st.columns([2, 2, 3])
    if left.button("Post the record to the pull request", key=kp + "post", disabled=not state["signoffs"]):
        try:
            url = flow.post_report(state)
        except flow.QAError as exc:
            st.error(str(exc))
            return
        st.session_state[S + "flash"] = [f"✅ Posted. [On the pull request ↗]({url})"]
        st.rerun()
    right.download_button("⬇ Download the record", flow.report(state), file_name=f"qa-pr-{state['number']}.md", key=kp + "dl")
    if state.get("posted_url"):
        st.caption(f"[Posted on the pull request ↗]({state['posted_url']})")
