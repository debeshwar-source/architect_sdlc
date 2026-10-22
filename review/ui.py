"""The code review screen: pick a pull request, read the AI's comments on the code, reword them, then post them to GitHub.

Plain words: choose one of the pull requests waiting for a merge -> the reviewer reads the diff and proposes comments, each shown
under the code it is about, like on GitHub -> you can reword, drop or keep every comment and the overall summary -> only when you
press "Post review" do the comments go onto the original pull request, as ONE review. Nothing reaches GitHub before that.
"""

from typing import Any, Dict, List

import streamlit as st
import streamlit.components.v1 as components

from coding import agent as coding_agent
from coding import diffview, testrun
from coding import workspace as ws
from review import github, loop, repos, reviewer

S = "rv_"                                 # session-state prefix


def _prs(repo: str, refresh: bool) -> List[Dict[str, Any]]:
    key = f"{S}prs:{repo.lower()}"
    if refresh or key not in st.session_state:
        owner, name = repo.split("/", 1)
        st.session_state[key] = github.list_open_prs(owner, name)
    return st.session_state[key]


def _summaries_panel(owner: str, name: str, number: int) -> None:
    """Summary comments (a review's main text) your account left on this PR. The app removes the ones it wrote itself when you start over;
    older ones cannot be told from a review summary you wrote by hand, so they are listed here for you to choose."""
    key = f"{S}summ_{owner}/{name}#{number}".lower()
    with st.expander("🧹 Summary comments from earlier reviews"):
        st.caption("Summary comments your account left on this pull request.")
        if st.button("Look for them", key=key + "_look"):
            try:
                st.session_state[key] = github.my_review_summaries(owner, name, number)
            except github.GithubError as exc:
                st.error(str(exc))
                return
        found = st.session_state.get(key)
        if found is None:
            return
        if not found:
            st.success("None found.")
            return
        picked = []
        for r in found:
            c1, c2 = st.columns([1, 9])
            on = c1.checkbox("Remove", value=r["ours"], key=f"{key}_{r['id']}")
            c2.markdown(("🤖 *written by this app*  \n" if r["ours"] else "") + f"[{r['state'].title()} review ↗]({r['url']}) · " + r["body"][:300])
            if on:
                picked.append(r)
        if st.button(f"Remove {len(picked)} selected", key=key + "_go", disabled=not picked, type="primary"):
            for line in loop.remove_summaries(owner, name, number, [{"id": r["id"], "node": r["node"]} for r in picked]):
                st.info(line)
            st.session_state.pop(key, None)


def _forget_review() -> None:
    """Drop the draft review kept in the session (the AI's proposed comments and their edits). It is used up once posted, and must never
    come back after the conversation is started over or undone."""
    st.session_state.pop(S + "data", None)


def _run_review(owner: str, name: str, pr: Dict[str, Any], model: str, on_event=None) -> None:
    diff_text = github.get_diff(owner, name, pr["number"])
    try:
        earlier, others = loop.split_open_threads(owner, name, pr["number"])
    except github.GithubError:
        earlier, others = [], []                        # cannot tell; the person can still post
    try:
        summaries = github.my_review_summaries(owner, name, pr["number"])
    except github.GithubError:
        summaries = []
    # the reviewer is told what others already said (not this app's own earlier comments: those may be about to be deleted)
    result = reviewer.review_pr(owner, name, pr, diff_text, model=model or None, existing=others, on_event=on_event)
    run = st.session_state.get(S + "run", 0) + 1
    st.session_state[S + "run"] = run
    st.session_state[S + "data"] = {"repo": (owner, name), "pr": pr, "files": diffview.parse(diff_text), "run": run, "earlier": earlier, "summaries": summaries, **result}


def render(default_model: str = "") -> None:
    if not github.token():
        st.warning("Set `GITHUB_TOKEN` in `.env`, then reload.")
        return

    with st.expander("Add a repository to look in", expanded=False):
        extra = st.text_input("GitHub repository", placeholder="owner/name  or  https://github.com/owner/name", key=S + "repo")
        if extra.strip() and not github.parse_repo(extra):
            st.warning("That does not look like a repository address.")
        elif extra.strip():
            repos.remember(extra)
    repo_list = repos.known_repos()
    if not repo_list:
        st.info("No repository known yet. Add one above.")
        return

    _, right = st.columns([5, 1])
    refresh = right.button("↻ Refresh", key=S + "refresh", use_container_width=True)
    found: List[Dict[str, Any]] = []
    for repo in repo_list:
        try:
            found += [{**p, "repo": repo} for p in _prs(repo, refresh)]
        except github.GithubError as exc:
            st.warning(f"**{repo}**: {exc}")
    if not found:
        st.success(f"No open pull requests are waiting for a merge in {', '.join(repo_list)}.")
        return
    multi = len(repo_list) > 1
    options = [(p["repo"].lower(), p["number"]) for p in found]
    by_key = dict(zip(options, found))

    def label(k) -> str:
        p = by_key[k]
        mark = "💬 " if loop.load(*p["repo"].split("/", 1), p["number"]) else ""
        return f"{mark}{p['repo'] + ' · ' if multi else ''}#{p['number']}  {p['title']}  —  {p['author']}  ({p['branch']} → {p['base']})"

    choice = st.selectbox(f"Pull requests awaiting merge ({len(found)})", options, format_func=label, key=S + "pick")
    pr = by_key[choice]
    owner, name = pr["repo"].split("/", 1)
    number = pr["number"]
    st.markdown(f"[Open on GitHub ↗]({pr['url']})")
    if pr["body"].strip():
        with st.expander("Pull request description"):
            st.markdown(pr["body"])

    for line in st.session_state.pop(S + "flash", None) or []:        # what was just done, whichever screen follows
        (st.success if line.startswith("✅") else st.info)(line)
    _summaries_panel(owner, name, number)

    state = loop.load(owner, name, number)
    if state:
        _render_conversation(state, default_model)
        return

    if st.button("📥 Address the review comments already on this pull request", key=S + "load"):
        try:
            with st.spinner("Reading the comments on GitHub…"):
                threads = github.unresolved_threads(owner, name, number)
        except github.GithubError as exc:
            st.error(str(exc))
            return
        if threads:
            loop.start_from_github(owner, name, pr, threads)
            st.rerun()
        st.info("There are no unresolved review comments on this pull request.")

    if st.button("🔍 Review this pull request", type="primary", key=S + "go"):
        line = st.empty()
        with st.spinner("The reviewer is checking out the code, reading it and checking its findings. This takes a few minutes…"):
            try:
                _run_review(owner, name, pr, default_model, on_event=lambda t: line.caption(f"…{t[:140]}"))
            except (github.GithubError, ValueError, coding_agent.Incomplete) as exc:
                st.error(str(exc))
                return

    data = st.session_state.get(S + "data")
    if not data or data["pr"]["number"] != number or data["repo"] != (owner, name):
        return
    _render_review(data)


def _render_review(data: Dict[str, Any]) -> None:
    owner, name = data["repo"]
    pr, run = data["pr"], data["run"]
    k = f"{S}{run}_"
    files = {f.path: f for f in data["files"]}
    st.divider()
    for note in data["notes"]:
        st.warning(note)
    earlier = data.get("earlier") or []
    drop = False
    if earlier:
        st.warning(f"{len(earlier)} comment(s) from an earlier review by this app are still open. Posting adds to them.")
        drop = st.checkbox(f"Delete the {len(earlier)} earlier comment(s) first", value=True, key=k + "drop")
    old_sums = data.get("summaries") or []
    drop_sums: List[Dict[str, Any]] = []
    if old_sums:
        st.warning(f"{len(old_sums)} summary comment(s) from earlier reviews by your account are still on this pull request. Tick the ones to delete.")
        for r in old_sums:
            c1, c2 = st.columns([1, 9])
            if c1.checkbox("Delete", value=True, key=f"{k}sum_{r['id']}"):
                drop_sums.append(r)
            c2.markdown(f"[{r['state'].title()} review ↗]({r['url']}) · {r['body'][:240]}")
    if data.get("rejected"):
        with st.expander(f"🗑️ Findings the second look discarded ({len(data['rejected'])})"):
            for r in data["rejected"]:
                st.markdown(f"**`{r['path']}` line {r['line']}**: {r['body']}")
                st.caption("Why it was dropped: " + (r["reason"] or "not confirmed"))
    summary = st.text_area("Overall summary (posted as the review's main comment)", value=data["summary"], key=k + "summary", height=110)

    st.subheader(f"Comments on the code ({len(data['comments'])})")
    if not data["comments"]:
        st.info("The reviewer found nothing to comment on.")
    paths = sorted({c["path"] for c in data["comments"]})
    for path in paths:
        f = files[path]
        st.markdown(f"##### 📄 `{path}`  &nbsp; <span style='color:#1a7f37'>+{f.added}</span> <span style='color:#cf222e'>−{f.removed}</span>",
                    unsafe_allow_html=True)
        for c in [c for c in data["comments"] if c["path"] == path]:
            with st.container(border=True):
                st.code(reviewer.context_snippet(f, c["line"]) or f"line {c['line']}", language="diff")
                if c.get("evidence"):
                    st.caption(("✅ Checked against the code: " if c.get("verified") else "Not double-checked: ") + c["evidence"])
                keep_col, text_col = st.columns([1, 9])
                keep_col.checkbox("Post", value=True, key=f"{k}keep_{c['id']}")
                text_col.text_area(f"Comment on line {c['line']}", value=c["body"], key=f"{k}body_{c['id']}", height=100,
                                   label_visibility="collapsed")

    chosen = [c for c in data["comments"] if st.session_state.get(f"{k}keep_{c['id']}", True)]
    st.divider()
    event = st.radio("Review type", list(github.EVENTS), key=k + "event", horizontal=True,
                     format_func={"COMMENT": "Comment only", "REQUEST_CHANGES": "Request changes"}.get)
    if not chosen and not summary.strip():
        st.caption("Nothing to post.")
        return
    if st.button("✅ Approve and post review to GitHub", type="primary", key=k + "post"):
        edited = [{**c, "body": st.session_state.get(f"{k}body_{c['id']}", c["body"]).strip()} for c in chosen]
        edited = [c for c in edited if c["body"]]
        try:
            if github.get_pr(owner, name, pr["number"])["head_sha"] != pr["head_sha"]:
                st.error("The pull request has new commits since this review, so these comments may no longer fit the code. "
                         "Refresh and review it again.")
                return
            if drop:
                loop.clear_comments(owner, name, earlier)           # before posting: a failure here leaves GitHub as it was
            gone = loop.remove_summaries(owner, name, pr["number"], [{"id": r["id"], "node": r["node"]} for r in drop_sums])
            posted = github.post_review(owner, name, pr["number"], pr["head_sha"], summary.strip(), event, edited)
        except (github.GithubError, loop.LoopError) as exc:
            st.error(str(exc))
            return
        loop.start(owner, name, pr, posted, edited)
        n = len(edited)
        st.session_state[S + "flash"] = ([f"✅ Posted {n} comment{'s' if n != 1 else ''}"
                                          + (" and the summary" if summary.strip() else "")
                                          + f" on pull request #{pr['number']}. [See the review ↗]({posted['url']})"]
                                         if posted.get("url") else [f"✅ Posted {n} comment(s) on pull request #{pr['number']}."]) + gone
        _forget_review()
        st.rerun()


# ---------- the conversation: coding agent <-> reviewer ----------

STATUS = {"open": "🔴 Open", "fixed": "🟡 Fixed: waiting for the reviewer", "disputed": "🟠 Disputed: waiting for the reviewer",
          "resolved": "🟢 Resolved", "skipped": "⏭️ Skipped: set aside by you", "untracked": "⚪ Not tracked (GitHub did not return its id)"}
WHO = {"reviewer": ("🔍", "assistant"), "coder": ("🛠️", "user"), "human": ("🧑", "user")}


def _working(label: str, fn, *args, events: bool = False, **kwargs) -> bool:
    """Run one long step with a spinner (and, if `events`, the agent's latest action); an error becomes a message. True on success."""
    line = st.empty()
    try:
        with st.spinner(label):
            fn(*args, **kwargs, **({"on_event": lambda t: line.caption(f"…{t[:140]}")} if events else {}))
    except (loop.LoopError, github.GithubError, ValueError, coding_agent.Incomplete) as exc:
        st.error(str(exc))
        return False
    return True


def _render_conversation(state: Dict[str, Any], model: str) -> None:
    owner, name = state["repo"]
    comments = [c for c in state["comments"] if c["status"] != "untracked"]
    done = sum(c["status"] == "resolved" for c in comments)
    kp = f"{S}{state['number']}_{state['round']}_{'f' if state.get('fix') else 'r' if state.get('recheck') else 'x'}_"
    top = st.columns([3, 2, 1])
    top[0].markdown(f"##### Conversation on #{state['number']} · round {state['round'] + 1} · **{done} of {len(comments)} comments resolved**")
    wipe = top[1].checkbox("Also delete this app's comments on GitHub", value=True, key=kp + "wipe")
    if top[2].button("Start over", key=kp + "reset"):
        try:
            out = loop.start_over(state, wipe)
        except (loop.LoopError, github.GithubError) as exc:
            st.session_state[S + "startover_error"] = str(exc)
        else:
            st.session_state.pop(S + "startover_error", None)
            _forget_review()
            st.session_state[S + "flash"] = ([f"Started over. {out['deleted']} comment(s) were deleted from GitHub."] if wipe else ["Started over."]) + out["notes"]
            st.rerun()
    if st.session_state.get(S + "startover_error"):
        st.error("GitHub did not let me clear everything, so nothing was forgotten: " + st.session_state[S + "startover_error"])
        if st.button("Start over without touching GitHub", key=kp + "reset_local"):
            loop.start_over(state, False)
            st.session_state.pop(S + "startover_error", None)
            _forget_review()
            st.session_state[S + "flash"] = ["Started over. Comments already on GitHub were left as they are."]
            st.rerun()
    for note in state.get("notes") or []:
        st.warning(note)
    if state.get("error"):
        st.error(state["error"])
    if loop.open_comments(state, "untracked"):
        _reconnect(state, kp)

    with st.expander(f"All comments ({len(state['comments'])})", expanded=not (state.get("fix") or state.get("recheck"))):
        for c in state["comments"]:
            with st.container(border=True):
                st.markdown(f"**`{c['path']}` line {c['line']}** · {STATUS[c['status']]}")
                st.markdown(c["body"])
                for m in c["thread"]:
                    icon, role = WHO.get(m["who"], ("💬", "assistant"))
                    with st.chat_message(role, avatar=icon):
                        st.markdown(m["text"])
                _comment_actions(state, c, kp, model)

    _undo_panel(state, kp)
    if state.get("fix"):
        _gate_fix(state, kp, model)
    elif state.get("recheck"):
        _gate_recheck(state, kp, model)
    elif state["phase"] == "unlinked":
        st.info("None of these comments is linked to a GitHub thread. Reconnect them above, or start over.")
    elif state["phase"] == "merged":
        st.success(f"🎉 This pull request was merged (commit `{state['merged']['sha'][:8]}`).")
        st.markdown(f"[Open the pull request ↗](https://github.com/{owner}/{name}/pull/{state['number']})")
    else:
        _next_steps(state, kp, model)


def _next_steps(state: Dict[str, Any], kp: str, model: str) -> None:
    """What the person can do now, whatever the state of the comments: the loop goes on for as long as they want."""
    owner, name = state["repo"]
    live = [c for c in state["comments"] if c["status"] != "untracked"]
    count = lambda *st_: sum(c["status"] in st_ for c in live)
    st.markdown("---")
    st.markdown(f"**Where things stand:** {count('resolved')} resolved · {count('open')} open · {count('fixed', 'disputed')} answered, waiting for the "
                f"reviewer · {count('skipped')} skipped")
    if state["phase"] == "approved":
        st.success("Every comment is settled" + (f" ({count('skipped')} skipped)" if count("skipped") else "") + ".")
    if count("open"):
        n = count("open")
        st.info(f"{n} comment(s) still open.")
        guidance = st.text_area("Note for the coding agent (optional)", key=kp + "guide", height=70)
        if st.button(f"🛠️ Let the coding agent address all {n} open comment(s)", type="primary" if not count("fixed", "disputed") else "secondary", key=kp + "fix"):
            if _working("The coding agent is working on the branch…", loop.run_fix, state, guidance, model or None, events=True):
                st.rerun()
    _review_again(state, kp, model)
    _merge_panel(state, kp)


def _review_again(state: Dict[str, Any], kp: str, model: str) -> None:
    """Pick which comments the reviewer should look at again, against the code as it is now. The ones answered by the coding agent are ticked;
    a comment you set aside can be sent back too."""
    cands = [c for c in state["comments"] if c["status"] in ("fixed", "disputed", "skipped") and c["gh_id"]]
    if not cands:
        return
    st.markdown("**🔍 Ask the reviewer to look again**")
    st.caption("Tick the comments to send back.")
    picked = []
    for c in cands:
        label = {"fixed": "fixed by the coding agent", "disputed": "disputed by the coding agent", "skipped": "skipped by you"}[c["status"]]
        if st.checkbox(f"`{c['path']}` line {c['line']} · {label}", value=c["status"] != "skipped", key=f"{kp}again_{c['id']}"):
            picked.append(c["id"])
    if st.button(f"🔍 Ask the reviewer to review the {len(picked)} selected again", key=kp + "recheck", disabled=not picked,
                 type="primary" if not loop.open_comments(state, "open") else "secondary"):
        if _working("The reviewer is re-reading the code…", loop.run_recheck, state, model or None, events=True, only=picked):
            st.rerun()


def _merge_panel(state: Dict[str, Any], kp: str) -> None:
    """Merge the pull request from here, through GitHub's own merge (so required checks and reviews still apply). Whatever you would be
    overriding is listed first, and overriding takes a typed confirmation."""
    owner, name = state["repo"]
    key = f"{S}merge_{owner}/{name}#{state['number']}".lower()
    with st.expander("🔀 Merge this pull request", expanded=state["phase"] == "approved"):
        st.caption("Merges through GitHub; its required checks still apply. Can't be undone.")
        if st.button("Check the merge status", key=kp + "mstatus"):
            try:
                st.session_state[key] = {"info": github.merge_info(owner, name, state["number"]),
                                         "threads": len(github.unresolved_threads(owner, name, state["number"]))}
            except github.GithubError as exc:
                st.error(str(exc))
                return
        got = st.session_state.get(key)
        if not got:
            return
        info, threads = got["info"], got["threads"]
        if info["merged"] or info["state"] != "open":
            st.info("This pull request is already " + ("merged." if info["merged"] else "closed."))
            return
        if info["draft"]:
            st.warning("This is a draft pull request; mark it ready for review on GitHub first.")
            return
        over = []
        if loop.unsettled(state):
            over.append(f"{len(loop.unsettled(state))} comment(s) here are not resolved ({count_by(state)})")
        if threads:
            over.append(f"{threads} unresolved conversation(s) on GitHub")
        if info["failing"]:
            over.append("failing checks: " + ", ".join(info["failing"]))
        if info["pending"]:
            over.append("checks still running: " + ", ".join(info["pending"]))
        if not info["checks_known"]:
            over.append("the state of the checks could not be read")
        if info["mergeable"] is False or info["mergeable_state"] in ("dirty", "blocked", "behind"):
            over.append(f"GitHub reports the pull request as “{info['mergeable_state']}”" + (" (conflicts)" if info["mergeable"] is False else ""))
        if info["head_sha"] != state["head_sha"]:
            over.append("commits were pushed after the last review of this pull request")
        if over:
            st.warning("**Merging now would override:**\n" + "\n".join(f"- {o}" for o in over))
        else:
            st.success("Everything is settled, the checks pass and GitHub reports no conflicts.")
        method = st.radio("Merge method", info["methods"], horizontal=True, key=kp + "mmethod",
                          format_func={"merge": "Merge commit", "squash": "Squash and merge", "rebase": "Rebase and merge"}.get)
        if over:
            ok = st.text_input("Type “merge” to merge anyway", key=kp + "mtype").strip().lower() == "merge"
        else:
            ok = st.checkbox("I confirm: merge this pull request", key=kp + "mok")
        if st.button("🔀 Merge now", type="primary", key=kp + "mgo", disabled=not ok):
            try:
                out = github.merge_pr(owner, name, state["number"], info["head_sha"], method)
            except github.GithubError as exc:
                st.error(str(exc))
                return
            if not out["merged"]:
                st.error(out["message"] or "GitHub did not merge it.")
                return
            loop.mark_merged(state, out["sha"])
            st.session_state.pop(key, None)
            st.session_state[S + "flash"] = [f"✅ Merged pull request #{state['number']} (commit `{out['sha'][:8]}`)"
                                             + (f", overriding {len(over)} warning(s)." if over else ".")]
            st.rerun()


def count_by(state: Dict[str, Any]) -> str:
    names = {"open": "open", "fixed": "waiting for the reviewer", "disputed": "disputed", "skipped": "skipped"}
    counts: Dict[str, int] = {}
    for c in loop.unsettled(state):
        counts[names[c["status"]]] = counts.get(names[c["status"]], 0) + 1
    return ", ".join(f"{n} {k}" for k, n in counts.items())


def _reconnect(state: Dict[str, Any], kp: str) -> None:
    """Some comments lost their link to their thread on GitHub, so nothing can be done with them. Try to find the threads again, once
    automatically, then on request."""
    owner, name = state["repo"]
    tried = f"{S}relink_{state['number']}"

    def attempt() -> None:
        st.session_state[tried] = True                  # one automatic try per session; after that, on request
        try:
            n = loop.relink(state, github.unresolved_threads(owner, name, state["number"]))
        except github.GithubError as exc:
            st.error(str(exc))
            return
        if n:
            st.rerun()
        st.warning("None could be matched to a thread on GitHub.")

    if not st.session_state.get(tried):
        attempt()
        return
    st.warning(f"{len(loop.open_comments(state, 'untracked'))} comment(s) are not linked to a GitHub thread.")
    if st.button("🔗 Reconnect them to GitHub", key=kp + "relink"):
        attempt()


def _comment_actions(state: Dict[str, Any], c: Dict[str, Any], kp: str, model: str) -> None:
    """The choices for ONE comment: ask the coding agent to fix it, answer and resolve it yourself, or set it aside."""
    if state.get("fix") or state.get("recheck") or state["phase"] == "merged":
        return                                         # something is waiting for your approval: finish that first
    status = c["status"]
    if status not in ("open", "fixed", "disputed", "skipped"):
        return
    flag = f"{S}resp_{state['number']}_{c['id']}"
    cols = st.columns([2, 2, 1, 1, 3])
    if status == "open" and cols[0].button("🛠️ Ask the coding agent", key=f"{kp}fix1_{c['id']}"):
        if _working("The coding agent is working on this comment…", loop.run_fix, state, st.session_state.get(kp + "guide", ""),
                    model or None, events=True, only=[c["id"]]):
            st.rerun()
    if c["gh_id"] and cols[1].button("💬 Respond & resolve", key=f"{kp}resp_{c['id']}"):
        st.session_state[flag] = not st.session_state.get(flag, False)
    if status == "open" and cols[2].button("Skip", key=f"{kp}skip_{c['id']}"):
        loop.skip_comment(state, c["id"])
        st.rerun()
    if status == "skipped" and cols[2].button("↶ Un-skip", key=f"{kp}unskip_{c['id']}"):
        loop.unskip_comment(state, c["id"])
        st.rerun()
    if st.session_state.get(flag):
        text = st.text_area("Your reply (posted in the thread, then the thread is marked resolved)", value="Addressed.", key=f"{kp}rtext_{c['id']}", height=80)
        if st.button("Post the reply and resolve", type="primary", key=f"{kp}rgo_{c['id']}", disabled=not text.strip()):
            try:
                loop.respond_and_resolve(state, c["id"], text)
            except (loop.LoopError, github.GithubError) as exc:
                st.error(str(exc))
                return
            st.session_state.pop(flag, None)
            st.session_state[S + "flash"] = [loop.last_summary(state)] + (state.get("notes") or [])
            st.rerun()


def _undo_panel(state: Dict[str, Any], kp: str) -> None:
    """One undo button per thing done so far, newest first. Each undoes exactly that, on this computer and on GitHub; only the newest can
    be pressed, because a later step may rest on an earlier one."""
    done = loop.actions(state)
    if not done:
        return
    blocker = loop.undo_blocker(state)
    with st.expander(f"↶ Undo ({len(done)} done so far)"):
        for i, a in enumerate(done):
            newest = i == 0
            why = blocker if newest else "Undo the newer one above first."
            c1, c2 = st.columns([3, 2])
            c1.markdown(f"{a['at']} · {a['label']}")
            sure = c2.checkbox("Also remove it from GitHub", key=f"{kp}undo_ok_{i}", disabled=bool(why)) if newest else False
            if st.button(f"↶ Undo {a['label']}", key=f"{kp}undo_{i}", disabled=bool(why) or not sure):
                try:
                    loop.undo_last(state)
                except (loop.LoopError, github.GithubError) as exc:
                    st.error(str(exc))
                    return
                _forget_review()
                st.session_state[S + "flash"] = state.get("undo_notes") or []
                st.rerun()
            if why and newest:
                st.caption(why)


def _gate_fix(state: Dict[str, Any], kp: str, model: str) -> None:
    """Gate 1: the coding agent's change and replies, for a person to read, edit and approve."""
    fix = state["fix"]
    st.subheader("🛠️ The coding agent's answer")
    st.markdown(fix["summary"] or "_No summary._")
    st.caption("Addressing: " + ", ".join(f"`{c['path']}`:{c['line']}" for c in state["comments"] if c["id"] in (fix.get("targets") or [])))
    tests = fix.get("tests") or {}
    report = tests.get("report")
    st.markdown("**🧪 The project's tests, run here on this change**")
    if report:
        if report.get("ran") and not (report.get("failed") or report.get("build_failed")):
            st.success(testrun.headline(report))
        else:
            st.error(testrun.headline(report))
            for c in report.get("failed_cases") or []:
                st.markdown(f"- `{c['name']}`" + (f": {c['message']}" if c.get("message") else ""))
            if report.get("tail"):
                with st.expander("Output (last lines)"):
                    st.code(report["tail"], language="text")
    else:
        st.warning(tests.get("note") or "The tests were not run.")
    cmd_col, run_col = st.columns([4, 1])
    cmd = cmd_col.text_input("Test command", value=tests.get("command") or state.get("test_command") or "", key=kp + "testcmd")
    run_col.write("")
    if run_col.button("↻ Run again", key=kp + "runtests", disabled=not cmd.strip()):
        if _working("Running the tests…", loop.run_tests, state, cmd):
            st.rerun()
    blocked = loop.tests_block(state)
    override = bool(blocked) and st.checkbox("These failures are not caused by this change: let me approve anyway", key=kp + "override")
    try:
        files = diffview.parse(loop.fix_diff(state))
    except loop.LoopError as exc:
        st.error(str(exc))
        files = []
    if files:
        components.html(diffview.render(files), height=diffview.height(files), scrolling=True)
    elif any(o["outcome"] == "fixed" for o in fix["outcomes"]):
        st.warning("No file changed.")
    by_id = {c["id"]: c for c in state["comments"]}
    for o in fix["outcomes"]:
        c = by_id[o["id"]]
        label = {"fixed": "✅ Fixed", "disagree": "🗣️ Disagrees", "needs_input": "❓ Needs your decision"}[o["outcome"]]
        with st.container(border=True):
            st.markdown(f"**`{c['path']}` line {c['line']}** · {label}")
            st.caption(c["body"])
            st.text_area("Reply posted in the GitHub thread" if o["outcome"] != "needs_input" else "Its question (not posted; answer it below)",
                         value=o["reply"], key=f"{kp}reply_{o['id']}", height=80, disabled=o["outcome"] == "needs_input")
    if st.button("↶ Discard the coding agent's change", key=kp + "discard_fix"):
        try:
            loop.discard_fix(state)
        except (loop.LoopError, ws.GitError) as exc:
            st.error(str(exc))
            return
        st.rerun()
    message = st.text_input("Commit message", value=f"Address review comments (round {state['round'] + 1})", key=kp + "msg")
    left, right = st.columns(2)
    if blocked and not override:
        left.caption("Approval is blocked until the tests pass.")
    if left.button("✅ Approve: commit, push and post the replies", type="primary", key=kp + "approve", disabled=bool(blocked) and not override):
        replies = {o["id"]: st.session_state.get(f"{kp}reply_{o['id']}", o["reply"]) for o in fix["outcomes"]}
        try:
            loop.approve_fix(state, replies, message, allow_failing_tests=override)
        except (loop.LoopError, github.GithubError) as exc:
            st.error(str(exc))
            return
        st.session_state[S + "flash"] = [loop.last_summary(state)] + (state.get("notes") or [])
        st.rerun()
    with right:
        guidance = st.text_area("Or tell the coding agent what to do differently / answer its question", key=kp + "again_note", height=70)
        if st.button("↻ Ask the coding agent again", key=kp + "again", disabled=not guidance.strip()):
            if _working("The coding agent is working…", loop.run_fix, state, guidance, model or None, events=True):
                st.rerun()


def _gate_recheck(state: Dict[str, Any], kp: str, model: str) -> None:
    """Gate 2: the reviewer's verdict on each answered comment, for a person to read, edit and approve."""
    rc = state["recheck"]
    st.subheader("🔍 The reviewer's second look")
    for note in rc.get("notes") or []:
        st.warning(note)
    summary = st.text_area("Summary (posted only with new comments, if there are any)", value=rc["summary"], key=kp + "summary", height=80)
    by_id = {c["id"]: c for c in state["comments"]}
    for v in rc["verdicts"]:
        c = by_id[v["id"]]
        with st.container(border=True):
            st.markdown(f"**`{c['path']}` line {c['line']}**")
            for m in c["thread"][-2:]:
                icon, role = WHO.get(m["who"], ("💬", "assistant"))
                with st.chat_message(role, avatar=icon):
                    st.markdown(m["text"])
            st.checkbox("Resolved", value=v["resolved"], key=f"{kp}res_{v['id']}")
            st.text_area("Reviewer's reply", value=v["reply"], key=f"{kp}reply_{v['id']}", height=70)
    kept: List[Dict[str, Any]] = []
    if rc["comments"]:
        st.markdown(f"**New comments about the fix ({len(rc['comments'])})**")
    for c in rc["comments"]:
        with st.container(border=True):
            st.markdown(f"`{c['path']}` line {c['line']}")
            keep = st.checkbox("Post", value=True, key=f"{kp}keep_{c['id']}")
            body = st.text_area("New comment", value=c["body"], key=f"{kp}body_{c['id']}", height=80, label_visibility="collapsed")
            if keep and body.strip():
                kept.append({**c, "body": body.strip()})
    if st.button("↶ Discard the reviewer's re-check", key=kp + "discard_recheck"):
        loop.discard_recheck(state)
        st.rerun()
    if st.button("✅ Approve: post the replies and resolve the settled threads", type="primary", key=kp + "approve"):
        resolved = {v["id"]: bool(st.session_state.get(f"{kp}res_{v['id']}", v["resolved"])) for v in rc["verdicts"]}
        replies = {v["id"]: st.session_state.get(f"{kp}reply_{v['id']}", v["reply"]) for v in rc["verdicts"]}
        try:
            loop.approve_recheck(state, resolved, replies, kept, summary)
        except (loop.LoopError, github.GithubError) as exc:
            st.error(str(exc))
            return
        st.session_state[S + "flash"] = [loop.last_summary(state)] + (state.get("notes") or [])
        st.rerun()
