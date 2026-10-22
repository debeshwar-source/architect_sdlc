"""Checks of the QA agent after a merge (qa/). Run: pytest test_qa.py

The AI, GitHub and git are never called: the QA agent and the checkout are replaced by fakes, so what is checked is the logic, above all
the human gates (nothing is checked unless a person chose it, nothing is signed off unless a person does it).
"""

import json

import pytest

import config
from coding import agent as coding_agent
from qa import agent, flow, plan as qa_plan
from review import checkout, github


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(flow, "STORE", tmp_path / "qa")
    qa_plan._cache.clear()

    def offline(*a, **k):
        raise github.GithubError("offline in tests")
    monkeypatch.setattr(github, "_request", offline)

    def no_checkout(*a, **k):
        raise checkout.CheckoutError("offline in tests")
    monkeypatch.setattr(checkout, "prepare_commit", no_checkout)


CASES = [
    {"id": "TC-1", "title": "Entry point exists", "component": "mobile-app", "type": "Functional", "priority": "High",
     "steps": ["Open Profile"], "expected_result": "A field is shown", "requirement_refs": ["AC-1"]},
    {"id": "TC-2", "title": "Verification before grant", "component": "d2c_user", "type": "Integration", "priority": "Critical",
     "steps": ["Submit a valid code"], "expected_result": "Verified first", "requirement_refs": ["AC-2"], "preconditions": "user has no plan"},
    {"id": "TC-3", "title": "Expiry is 30 days", "component": "d2c_user", "type": "Functional", "priority": "Medium",
     "steps": ["Redeem"], "expected_result": "Expires in 30 days", "requirement_refs": ["AC-2"]},
    {"id": "TC-4", "title": "Share sheet wording", "component": "mobile-app", "type": "Usability", "priority": "Medium",
     "steps": ["Tap share"], "expected_result": "Excited wording", "requirement_refs": []}]


def make_run(runs_root, run_id="20261015-102919-aaaaaa", cases=CASES):
    d = runs_root / run_id
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps({"pipeline_state": {
        "requirement": {"title": "Access coupons", "acceptance_criteria": ["Profile has an entry point.", "Submitting triggers verification."]},
        "quality_plan": {"summary": "Covers coupons.", "test_cases": cases}}}))
    return run_id


PR = {"number": 7, "title": "Coupon purchase verify", "body": "Adds verify.", "branch": "feature/coupon-purchase", "base": "dev", "url": "https://x/pull/7",
      "merge_sha": "abcdef1234567890", "merged_at": "2026-10-19T10:00:00Z", "author": "me"}


def good_plan():
    return qa_plan.load_plan(make_run(config.RUNS_ROOT))


# ---------- the test plan ----------

def test_the_plan_is_read_from_a_run_with_acceptance_criteria_numbered(tmp_path):
    p = good_plan()
    assert p["title"] == "Access coupons" and [c["id"] for c in p["test_cases"]] == ["TC-1", "TC-2", "TC-3", "TC-4"]
    assert p["acceptance_criteria"] == [{"id": "AC-1", "text": "Profile has an entry point."}, {"id": "AC-2", "text": "Submitting triggers verification."}]
    assert qa_plan.runs_with_plans() == [{"run_id": "20261015-102919-aaaaaa", "title": "Access coupons", "cases": 4}]
    assert qa_plan.load_plan("nope") is None
    (config.RUNS_ROOT / "bare").mkdir()
    (config.RUNS_ROOT / "bare" / "session.json").write_text(json.dumps({"pipeline_state": {"quality_plan": {"test_cases": []}}}))
    assert qa_plan.load_plan("bare") is None                                                     # a run without test cases has no plan


class _Job:
    def __init__(self, source, branch, run_id):
        from coding import workspace as ws
        self.d = {"ws": {"source": source}, "task": {"run_id": run_id}}
        self.workspace = ws.Workspace(None, None, branch, "dev", "x", source)


def test_a_merged_pr_is_matched_to_its_run_by_repository_and_branch(monkeypatch):
    from coding import flow as coding_flow
    run = make_run(config.RUNS_ROOT)
    other = make_run(config.RUNS_ROOT, "20260101-000000-bbbbbb")
    monkeypatch.setattr(coding_flow, "list_jobs", lambda task_key=None: [                       # newest first
        _Job("https://github.com/Other/Repo.git", "feature/coupon-purchase", other),
        _Job("https://github.com/MythikAI/D2CUser_debesh.git", "feature/coupon-purchase", run),
        _Job("https://github.com/MythikAI/D2CUser_debesh", "feature/something-else", other)])
    assert qa_plan.find_run_for_pr("mythikai", "d2cuser_debesh", "feature/coupon-purchase") == run
    assert qa_plan.find_run_for_pr("mythikai", "d2cuser_debesh", "feature/unknown") is None


# ---------- the QA agent's answers are cleaned, never trusted blindly ----------

def test_a_test_case_the_agent_skipped_is_not_assumed_checkable():
    out = agent.clean_assessment({"summary": "s", "items": [
        {"id": "TC-2", "verdict": "can_verify", "method": "both", "plan": "Read the controller; run the unit tests.", "commands": ["./mvnw test"]},
        {"id": "TC-3", "verdict": "partial", "method": "bogus", "plan": "x", "blockers": "needs a db"},
        {"id": "TC-4", "verdict": "banana"}]}, CASES)["items"]
    assert [(i, out[i]["verdict"], out[i]["checkable"]) for i in out] == [
        ("TC-1", "cannot_verify", False), ("TC-2", "can_verify", True), ("TC-3", "partial", True), ("TC-4", "cannot_verify", False)]
    assert "no usable assessment" in out["TC-1"]["blockers"] and out["TC-3"]["method"] == "none" and out["TC-2"]["commands"] == ["./mvnw test"]


def test_a_pass_without_proof_is_not_a_pass_and_a_fail_always_names_an_issue():
    unproven = agent.clean_result({"result": "pass", "summary": "Looks right.", "evidence": [], "issues": []})
    assert unproven["result"] == "inconclusive" and "No proof" in unproven["summary"]
    failed = agent.clean_result({"result": "fail", "summary": "Expiry is computed from purchase, not redemption.", "evidence": ["A.java:10"], "issues": []})
    assert failed["result"] == "fail" and failed["issues"][0]["severity"] == "major" and "Expiry" in failed["issues"][0]["title"]
    ok = agent.clean_result({"result": "pass", "summary": "ok", "evidence": ["  A.java:10   verifies first "], "issues": [{"severity": "weird", "title": "t", "detail": "d"}]})
    assert ok["result"] == "pass" and ok["evidence"] == ["A.java:10 verifies first"] and ok["issues"][0]["severity"] == "major"
    assert agent.clean_result({"result": "???", "summary": "", "evidence": [], "issues": []})["result"] == "inconclusive"


class FakeCall:
    def __init__(self, monkeypatch, answers):
        self.calls, self.answers = [], answers
        monkeypatch.setattr(coding_agent, "_call", self)

    def __call__(self, label, role, prompt, system, cwd, model, resume, on_event, schema, tools, disallowed, max_turns=80):
        self.calls.append({"label": label, "prompt": prompt, "system": system, "cwd": cwd, "tools": tools, "disallowed": disallowed})
        return {"answer": self.answers[label], "session_id": "s"}


def test_the_assessment_gives_the_agent_everything_and_tools_that_cannot_change_the_code(monkeypatch):
    fake = FakeCall(monkeypatch, {"qa-assess": {"summary": "s", "items": []}})
    files = [{"path": "src/A.java", "status": "added", "added": 10, "removed": 0}]
    agent.assess(PR, good_plan(), files, "diff --git a/x b/x\n" + "x" * 70_000, "/tree", on_event=lambda t: None)
    call = fake.calls[0]
    for needle in ("TC-1", "TC-4", "AC-2: Submitting triggers verification.", "src/A.java (added, +10 -0)", "Coupon purchase verify", "diff cut"):
        assert needle in call["prompt"], needle
    assert call["cwd"] == "/tree" and set(call["tools"]) == {"Read", "Glob", "Grep"}               # assessing runs NOTHING: no shell until a person chose
    assert "Bash(git push:*)" in call["disallowed"] and "can be checked here" in call["system"] and "can NOT run anything" in call["system"]


def test_verifying_one_case_sends_only_that_case_its_criteria_and_the_persons_guidance(monkeypatch):
    fake = FakeCall(monkeypatch, {"qa-verify": {"result": "pass", "summary": "ok", "evidence": ["A.java:1"], "issues": []}})
    case = CASES[1]
    out = agent.verify_case(PR, case, good_plan(), {"plan": "Read the controller.", "blockers": "no db"}, "/tree", guidance="Focus on the retry.")
    p = fake.calls[0]["prompt"]
    assert "TC-2" in p and "TC-3" not in p and "AC-2: Submitting triggers verification." in p and "AC-1" not in p
    assert "Read the controller." in p and "no db" in p and "Focus on the retry." in p and out["result"] == "pass"
    assert set(fake.calls[0]["tools"]) == {"Read", "Glob", "Grep", "Bash"}                         # the approved check may run the project's own tests
    assert "second reviewer" not in fake.calls[0]["system"] and "ONE test case" in fake.calls[0]["system"]


# ---------- the flow, and the gates ----------

@pytest.fixture
def qa(monkeypatch, tmp_path):
    """A QA record for PR #7 with the QA agent, git and GitHub faked. `qa.verdicts` sets what each check finds; `qa.checked` lists what was run."""
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(checkout, "prepare_commit", lambda o, n, sha, remote=None: tree)
    monkeypatch.setattr(github, "pr_files", lambda *a: [{"path": "A.java", "status": "added", "added": 5, "removed": 0}])
    monkeypatch.setattr(github, "get_diff", lambda *a: "diff")

    class Box:
        checked, posted = [], []
        verdicts = {}
    box = Box()
    box.checked, box.posted, box.verdicts = [], [], {}

    def fake_assess(pr, plan, files, diff, tree_, model=None, on_event=None):
        return agent.clean_assessment({"summary": "Two can be checked.", "items": [
            {"id": "TC-1", "verdict": "cannot_verify", "blockers": "needs a phone"},
            {"id": "TC-2", "verdict": "can_verify", "method": "both", "plan": "Read the controller."},
            {"id": "TC-3", "verdict": "partial", "method": "code_inspection", "plan": "Read the date maths.", "blockers": "no clock control"},
            {"id": "TC-4", "verdict": "out_of_scope"}]}, plan["test_cases"])

    def fake_verify(pr, case, plan, assessed, tree_, model=None, guidance="", on_event=None):
        box.checked.append((case["id"], guidance))
        v = box.verdicts.get(case["id"], "pass")
        if isinstance(v, Exception):
            raise v
        return agent.clean_result({"result": v, "summary": f"{case['id']} {v}", "evidence": ["A.java:1"] if v == "pass" else [],
                                   "issues": [{"severity": "major", "title": "Wrong", "detail": "d", "where": "A.java:9"}] if v == "fail" else []})
    monkeypatch.setattr(agent, "assess", fake_assess)
    monkeypatch.setattr(agent, "verify_case", fake_verify)
    monkeypatch.setattr(github, "comment_on_pr", lambda o, n, num, body: (box.posted.append(body), "https://x/pull/7#c1")[1])
    box.state = flow.start("o", "r", PR, good_plan())
    return box


def test_a_record_starts_empty_and_survives_a_restart(qa):
    s = qa.state
    assert flow.phase(s) == "select" and flow.load("o", "r", 7)["run_id"] == "20261015-102919-aaaaaa" and len(s["cases"]) == 4
    flow.run_assess(s)
    assert flow.phase(flow.load("O", "R", 7)) == "select"                                        # reloaded from disk (names are case-insensitive)


def test_assessing_suggests_what_can_be_checked_most_important_first_but_checks_nothing(qa):
    flow.run_assess(qa.state)
    assert qa.state["selected"] == ["TC-2", "TC-3"] and qa.checked == []                         # critical before medium; no check was run
    assert qa.state["assessment"]["items"]["TC-1"]["checkable"] is False and qa.state["results"] == {}


def test_assessment_failures_are_kept_and_explained_and_a_retry_works(qa, monkeypatch):
    s = qa.state
    s["pr"]["merge_sha"] = ""
    with pytest.raises(flow.QAError, match="no merge commit"):
        flow.run_assess(s)
    s["pr"]["merge_sha"] = "abc"
    monkeypatch.setattr(checkout, "prepare_commit", lambda *a: (_ for _ in ()).throw(checkout.CheckoutError("GitHub wants a login")))
    with pytest.raises(flow.QAError, match="wants a login"):
        flow.run_assess(s)
    assert flow.load("o", "r", 7)["error"] == "GitHub wants a login" and flow.phase(s) == "select"


def test_only_what_the_person_chose_is_ever_checked(qa):
    s = qa.state
    flow.run_assess(s)
    flow.set_selection(s, ["TC-3", "TC-1", "TC-9"])                                              # TC-9 is not in the plan: ignored
    assert s["selected"] == ["TC-1", "TC-3"]
    flow.run_checks(s)
    assert [c for c, _ in qa.checked] == ["TC-1", "TC-3"] and set(s["results"]) == {"TC-1", "TC-3"}      # TC-2 was not chosen: never touched
    with pytest.raises(flow.QAError, match="already been checked"):
        flow.run_checks(s)
    flow.set_selection(s, [])
    with pytest.raises(flow.QAError, match="Nothing is chosen"):
        flow.run_checks(s)


def test_any_test_case_in_the_plan_can_be_run_or_redone_without_an_assessment(qa):
    s = qa.state
    flow.run_checks(s, only=["TC-4", "TC-1", "TC-9"])                                            # no assessment needed; TC-9 is not in the plan: ignored
    assert [c for c, _ in qa.checked] == ["TC-1", "TC-4"] and set(s["results"]) == {"TC-1", "TC-4"} and s["selected"] == ["TC-1", "TC-4"]
    flow.run_checks(s, only=["TC-4"], guidance="again")                                          # a redo
    assert qa.checked[-1] == ("TC-4", "again") and s["results"]["TC-4"]["attempt"] == 2
    with pytest.raises(flow.QAError, match="Nothing is chosen"):
        flow.run_checks(s, only=["TC-9"])


def test_a_stopped_agent_is_not_a_test_result_the_run_ends_and_earlier_results_stay(qa):
    s = qa.state
    flow.run_assess(s)
    qa.verdicts = {"TC-3": RuntimeError("Command failed with exit code 143")}                    # the agent process was killed during the 2nd check
    with pytest.raises(flow.QAError, match="stopped while checking TC-3"):
        flow.run_checks(s)
    assert set(s["results"]) == {"TC-2"} and s["results"]["TC-2"]["result"] == "pass"            # nothing recorded for the stopped one
    assert [c for c, _ in qa.checked] == ["TC-2", "TC-3"]                                        # and the run did not march on
    assert "Run the checks again" in flow.load("o", "r", 7)["error"] and any("run stopped at TC-3" in h for h in s["history"])
    qa.verdicts = {}
    flow.run_checks(s)                                                                           # starting again does only what is left
    assert set(s["results"]) == {"TC-2", "TC-3"} and s["error"] is None and flow.counts(s)["passed"] == 2


def test_a_check_that_honestly_ran_out_of_room_is_inconclusive_not_a_failure_of_the_run(qa):
    s = qa.state
    flow.run_assess(s)
    qa.verdicts = {"TC-2": coding_agent.Incomplete("The coding agent stopped without a final answer (it ran out of turns).")}
    flow.run_checks(s)
    assert s["results"]["TC-2"]["result"] == "inconclusive" and "ran out of room" in s["results"]["TC-2"]["summary"] and s["results"]["TC-3"]["result"] == "pass"


def test_a_recheck_runs_again_with_guidance_and_asks_for_a_new_decision(qa):
    s = qa.state
    flow.run_assess(s)
    qa.verdicts = {"TC-2": "fail"}
    flow.run_checks(s)
    flow.override_case(s, "TC-2", "Dee", "Known; fixed next sprint.")
    qa.verdicts = {"TC-2": "pass"}
    flow.run_checks(s, only=["TC-2"], guidance="Look at the retry again.")
    assert qa.checked[-1] == ("TC-2", "Look at the retry again.") and s["results"]["TC-2"]["attempt"] == 2 and s["results"]["TC-2"]["result"] == "pass"
    assert flow.decision_of(s, "TC-2") is None                                                   # a new result needs a new decision


def test_counts_follow_the_results_and_the_decisions(qa):
    s = qa.state
    flow.run_assess(s)
    assert flow.counts(s) == {"checked": 0, "passed": 0, "failed": 0, "inconclusive": 0, "signed_off": 0, "overridden": 0, "waiting": 0, "not_checked": 4}
    qa.verdicts = {"TC-3": "inconclusive", "TC-2": "fail"}
    flow.run_checks(s)
    flow.override_case(s, "TC-2", "Dee", "Behind a flag.")
    assert flow.counts(s) == {"checked": 2, "passed": 0, "failed": 1, "inconclusive": 1, "signed_off": 0, "overridden": 1, "waiting": 1, "not_checked": 2}


def test_not_covered_lists_what_nobody_checked_here(qa):
    flow.run_assess(qa.state)
    flow.run_checks(qa.state)
    assert flow.not_covered(qa.state) == ["TC-1", "TC-4"]


def test_only_a_failed_or_doubtful_check_can_be_overridden_and_a_name_and_a_reason_are_required(qa):
    s = qa.state
    flow.run_assess(s)
    qa.verdicts = {"TC-2": "fail"}
    flow.run_checks(s)
    with pytest.raises(flow.QAError, match="Only a failed"):
        flow.override_case(s, "TC-3", "Dee", "x")                                                # it passed (or was never checked): nothing to override
    with pytest.raises(flow.QAError, match="your name"):
        flow.override_case(s, "TC-2", "  ", "x")
    with pytest.raises(flow.QAError, match="Say why"):
        flow.override_case(s, "TC-2", "Dee", "  ")
    flow.override_case(s, "TC-2", "Dee", "Accepted: behind a flag.")
    d = flow.decision_of(s, "TC-2")
    assert d["decision"] == "overridden" and d["by"] == "Dee" and d["note"] == "Accepted: behind a flag." and d["qa_result"] == "fail"
    flow.take_back(s, "TC-2")
    assert flow.decision_of(s, "TC-2") is None
    with pytest.raises(flow.QAError, match="Nothing was decided"):
        flow.take_back(s, "TC-2")


# ---------- the human always decides, one test case at a time ----------

def test_a_pass_decides_nothing_until_a_person_signs_that_test_case_off_by_name(qa):
    s = qa.state
    flow.run_assess(s)
    flow.run_checks(s)
    assert flow.counts(s)["passed"] == 2 and flow.counts(s)["signed_off"] == 0 and flow.counts(s)["waiting"] == 2    # QA alone decides nothing
    with pytest.raises(flow.QAError, match="your name"):
        flow.sign_off_case(s, "TC-2", "  ")
    flow.sign_off_case(s, "TC-2", "Dee")
    d = flow.decision_of(s, "TC-2")
    assert d["decision"] == "signed_off" and d["by"] == "Dee" and d["qa_result"] == "pass"
    assert flow.decision_of(s, "TC-3") is None and flow.counts(s)["waiting"] == 1                                    # only that one: the others still wait
    assert flow.load("o", "r", 7)["signoffs"]["TC-2"]["by"] == "Dee"


def test_only_a_test_case_that_passed_can_be_signed_off(qa):
    s = qa.state
    flow.run_assess(s)
    qa.verdicts = {"TC-2": "fail", "TC-3": "inconclusive"}
    flow.run_checks(s)
    for tc in ("TC-2", "TC-3"):
        with pytest.raises(flow.QAError, match="did not pass"):
            flow.sign_off_case(s, tc, "Dee")                                                  # signing over a failure is not possible: override it instead
    with pytest.raises(flow.QAError, match="not been checked"):
        flow.sign_off_case(s, "TC-1", "Dee")
    assert s["signoffs"] == {}


def test_a_decision_on_one_test_case_never_touches_another(qa):
    s = qa.state
    flow.run_assess(s)
    qa.verdicts = {"TC-2": "fail"}
    flow.run_checks(s)
    flow.override_case(s, "TC-2", "Dee", "Behind a flag.")
    flow.sign_off_case(s, "TC-3", "Dee")
    flow.take_back(s, "TC-2")
    assert flow.decision_of(s, "TC-3")["decision"] == "signed_off" and flow.decision_of(s, "TC-2") is None
    flow.run_checks(s, only=["TC-3"])                                                              # a new check of TC-3 takes back TC-3's decision only
    assert flow.decision_of(s, "TC-3") is None and any("TC-3: its earlier decision was taken back" in h for h in s["history"])


def test_records_saved_with_a_whole_record_decision_or_accepted_risks_are_migrated(qa):
    s = flow.load("o", "r", 7)
    s["waivers"] = {"TC-2": {"reason": "Behind a flag.", "by": "Dee", "at": "2026-10-19 10:00:00", "was": "fail"}}
    s["signoff"] = {"decision": "signed_off", "by": "Dee", "at": "x", "note": "", "overrode": False}
    s.pop("signoffs", None)
    flow.save(s)
    got = flow.load("o", "r", 7)
    assert got["signoffs"] == {"TC-2": {"decision": "overridden", "by": "Dee", "at": "2026-10-19 10:00:00", "note": "Behind a flag.", "qa_result": "fail"}}
    assert "waivers" not in got and "signoff" not in got and any("whole-record decision" in h for h in got["history"])      # nothing is silently signed off



# ---------- the record, written out ----------

def test_the_report_says_for_each_test_case_what_was_found_and_decided(qa):
    s = qa.state
    flow.run_assess(s)
    qa.verdicts = {"TC-2": "fail"}
    flow.run_checks(s)
    flow.override_case(s, "TC-2", "Dee", "Behind a flag.")
    flow.sign_off_case(s, "TC-3", "Dee")
    md = flow.report(s)
    for expected in ("pull request #7", "Access coupons", "1 signed off · 1 overridden · 0 waiting for a decision · 2 not checked",
                     "| TC-2 Verification before grant | Critical | ❌ Fail TC-2 fail | Overridden by Dee: Behind a flag. |",
                     "| TC-3 Expiry is 30 days | Medium | ✅ Pass TC-3 pass | Signed off by Dee |", "**major** Wrong: d (`A.java:9`)",
                     "## Not checked here", "TC-1, TC-4"):
        assert expected in md, expected
    assert "SIGNED OFF" not in md and "REJECTED" not in md                                           # there is no whole-record decision


def test_the_record_goes_on_the_pull_request_only_after_a_decision_and_only_when_asked(qa, monkeypatch):
    s = qa.state
    flow.run_assess(s)
    flow.run_checks(s)
    with pytest.raises(flow.QAError, match="Decide on at least one"):
        flow.post_report(s)
    flow.sign_off_case(s, "TC-2", "Dee")
    assert qa.posted == []                                                                       # deciding posts nothing by itself
    assert flow.post_report(s) == "https://x/pull/7#c1" and "Signed off by Dee" in qa.posted[0] and flow.load("o", "r", 7)["posted_url"]
    monkeypatch.setattr(github, "comment_on_pr", lambda *a: (_ for _ in ()).throw(github.GithubError("no permission", 403)))
    with pytest.raises(flow.QAError, match="no permission"):
        flow.post_report(s)


# ---------- the screen ----------

def _script():
    from qa import ui
    ui.render()


@pytest.fixture
def screen(qa, monkeypatch):
    """The QA screen with a merged PR #7, a saved test plan, and the QA agent faked. Returns (box, open) where open() draws the screen."""
    from streamlit.testing.v1 import AppTest
    from review import repos
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    flow.discard(qa.state)                                                                       # the fixture's record: the person starts it here
    monkeypatch.setattr(repos, "known_repos", lambda: ["o/r"])
    monkeypatch.setattr(github, "list_merged_prs", lambda o, n, limit=20: [{**PR, "merged_at": "2026-10-19T10:00:00Z"}])
    monkeypatch.setattr(qa_plan, "find_run_for_pr", lambda o, n, b: "20261015-102919-aaaaaa")
    monkeypatch.setattr(flow, "_default_name", lambda: "Dee", raising=False)
    monkeypatch.setattr("qa.ui._default_name", lambda: "Dee")
    return qa, lambda: AppTest.from_function(_script, default_timeout=30).run()


def _click(at, key):
    next(b for b in at.button if b.key == key).click()
    return at.run()


def _labels(at):
    return [b.label for b in at.button]


def _start_and_run(open_screen, ids=("TC-2", "TC-3")):
    """Start QA on the merged PR, tick these test cases, and run QA on them."""
    at = _click(open_screen(), "qa_start")
    for i in ids:
        at.checkbox(key=f"qa_7_sel_{i}").check()
    at.run()
    return _click(at, "qa_7_run")


def test_screen_walks_from_a_merged_pr_to_a_sign_off_of_one_test_case(screen):
    box, open_screen = screen
    at = open_screen()
    assert not at.exception, at.exception
    assert "▶ Start QA for this pull request" in _labels(at) and any("Matched from the coding job" in c.value for c in at.caption)
    at = _click(at, "qa_start")
    boxes = {c.key: c.value for c in at.checkbox if "_sel_" in (c.key or "")}
    assert boxes == {"qa_7_sel_TC-1": False, "qa_7_sel_TC-2": False, "qa_7_sel_TC-3": False, "qa_7_sel_TC-4": False}     # every test case, none ticked
    assert next(b for b in at.button if b.key == "qa_7_run").disabled is True and box.checked == []                     # nothing runs by itself
    at.checkbox(key="qa_7_sel_TC-2").check().run()                                              # the person picks one, from all of them
    assert next(b for b in at.button if b.key == "qa_7_run").label == "▶ Run QA on 1 selected"
    at = _click(at, "qa_7_run")
    assert not at.exception, at.exception
    assert [c for c, _ in box.checked] == ["TC-2"]                                              # only what was ticked was checked
    assert any(m.value.startswith("Passed: ") for m in at.markdown) and flow.load("o", "r", 7)["signoffs"] == {}      # a pass says why, and decides nothing
    assert any("0 signed off · 0 overridden · 1 waiting · 3 not checked" in m.value for m in at.markdown)
    at.text_input(key="qa_7_name").set_value("  ").run()
    assert next(b for b in at.button if b.key == "qa_7_so_TC-2").disabled is True              # no name, no sign-off
    at.text_input(key="qa_7_name").set_value("Dee Mitra").run()
    at = _click(at, "qa_7_so_TC-2")                                                             # this test case, and only this one
    assert not at.exception, at.exception
    assert flow.load("o", "r", 7)["signoffs"]["TC-2"]["by"] == "Dee Mitra"
    assert any("Signed off by Dee Mitra" in c.value for c in at.caption)
    assert any("1 signed off · 0 overridden · 0 waiting · 3 not checked" in m.value for m in at.markdown)
    assert "✅ Sign off" not in _labels(at) and "↶ Take back" in _labels(at)
    at = _click(at, "qa_7_unw_TC-2")
    assert flow.load("o", "r", 7)["signoffs"] == {} and "✅ Sign off" in _labels(at)


def test_screen_there_is_no_sign_off_or_override_of_the_whole_record(screen):
    box, open_screen = screen
    box.verdicts = {"TC-2": "fail"}
    at = _start_and_run(open_screen)
    assert not [b for b in at.button if b.key in ("qa_7_sign", "qa_7_reject", "qa_7_reopen")]
    assert not [c for c in at.checkbox if c.key == "qa_7_override"] and not [m for m in at.markdown if m.value.startswith("### Decision")]
    assert not any("overrides QA" in w.value for w in at.warning)
    per_case = [(b.key, b.label) for b in at.button if (b.key or "").startswith(("qa_7_so_", "qa_7_ovr_"))]
    assert per_case == [("qa_7_ovr_TC-2", "⚖️ Override"), ("qa_7_so_TC-3", "✅ Sign off")]       # TC-2 failed: override; TC-3 passed: sign off. One each


def test_screen_overriding_a_failure_is_for_that_test_case_alone(screen):
    box, open_screen = screen
    box.verdicts = {"TC-2": "fail"}
    at = _start_and_run(open_screen)
    assert any(m.value.startswith("Failed: ") for m in at.markdown)
    at = _click(at, "qa_7_ovr_TC-2")
    assert next(b for b in at.button if b.key == "qa_7_wv_TC-2").disabled is True                # a reason is needed first
    at.text_input(key="qa_7_why_TC-2").set_value("Failure is behind a disabled flag.").run()
    at = _click(at, "qa_7_wv_TC-2")
    assert not at.exception, at.exception
    d = flow.load("o", "r", 7)["signoffs"]
    assert list(d) == ["TC-2"] and d["TC-2"]["decision"] == "overridden" and d["TC-2"]["note"] == "Failure is behind a disabled flag." and d["TC-2"]["qa_result"] == "fail"
    assert any("Overridden by Dee: Failure is behind a disabled flag." in c.value for c in at.caption)
    assert "✅ Sign off" in _labels(at)                                                           # TC-3 is untouched: still waiting for its own sign-off
    assert any("0 signed off · 1 overridden · 1 waiting · 2 not checked" in m.value for m in at.markdown)


def test_screen_the_record_can_be_posted_to_the_pull_request_and_downloaded(screen):
    box, open_screen = screen
    at = _start_and_run(open_screen)
    assert next(b for b in at.button if b.key == "qa_7_post").disabled is True                   # nothing decided yet: nothing to post
    at = _click(at, "qa_7_so_TC-2")
    assert next(b for b in at.button if b.key == "qa_7_post").disabled is False
    assert [b.label for b in at.get("download_button")] == ["⬇ Download the record"]
    at = _click(at, "qa_7_post")
    assert box.posted and "Signed off by Dee" in box.posted[0] and any("Posted" in s.value for s in at.success)


def test_screen_run_or_redo_any_test_case_on_its_own_or_in_bulk(screen):
    box, open_screen = screen
    at = _click(open_screen(), "qa_start")
    assert "#### Not assessed yet (4)" in [m.value for m in at.markdown] and "↻ Redo" not in _labels(at)
    at = _click(at, "qa_7_go_TC-3")                                                             # one test case, straight from its row
    assert [c for c, _ in box.checked] == ["TC-3"]
    assert next(b for b in at.button if b.key == "qa_7_go_TC-3").label == "↻ Redo"              # now it can be redone
    at.text_input(key="qa_7_guide").set_value("Check the timezone.").run()
    at = _click(at, "qa_7_go_TC-3")
    assert box.checked[-1] == ("TC-3", "Check the timezone.") and flow.load("o", "r", 7)["results"]["TC-3"]["attempt"] == 2
    at.checkbox(key="qa_7_sel_TC-3").check().run(); at.checkbox(key="qa_7_sel_TC-1").check().run()          # a redo and a first run, together
    assert next(b for b in at.button if b.key == "qa_7_run").label == "▶ Run QA on 2 selected (1 redo)"
    box.checked.clear()
    at = _click(at, "qa_7_run")
    assert sorted(c for c, _ in box.checked) == ["TC-1", "TC-3"] and set(flow.load("o", "r", 7)["results"]) == {"TC-1", "TC-3"}


def test_screen_select_all_and_clear(screen):
    box, open_screen = screen
    at = _click(open_screen(), "qa_start")
    at = _click(at, "qa_7_all")
    assert all(c.value for c in at.checkbox if "_sel" in (c.key or "")) and next(b for b in at.button if b.key == "qa_7_run").label == "▶ Run QA on 4 selected"
    at = _click(at, "qa_7_none")
    assert not any(c.value for c in at.checkbox if "_sel" in (c.key or ""))


def test_screen_groups_the_cases_so_it_is_clear_which_can_be_tested_which_cannot_and_which_are_done(screen):
    box, open_screen = screen
    at = _click(open_screen(), "qa_start")
    heads = lambda: [m.value for m in at.markdown if m.value.startswith("#### ")]
    assert heads() == ["#### Not assessed yet (4)"] and next(b for b in at.button if b.key == "qa_7_assess").label == "Find out which can be tested"
    at = _click(at, "qa_7_assess")                                                              # it only sorts them; nothing is run
    assert not at.exception, at.exception
    assert box.checked == [] and "qa_7_assess" not in [b.key for b in at.button]
    assert heads() == ["#### Can be checked (1)", "#### Partly (1)", "#### Can't be checked here (2)"]
    assert any("needs a phone" in c.value for c in at.caption) and any("Not part of this change" in c.value for c in at.caption)
    labels = {b.key: b.label for b in at.button if (b.key or "").startswith("qa_7_go_")}
    assert labels == {"qa_7_go_TC-2": "▶ Run", "qa_7_go_TC-3": "▶ Run", "qa_7_go_TC-1": "▶ Try", "qa_7_go_TC-4": "▶ Try"}   # the ones QA can't check say "Try"
    assert [c.value for c in sorted(at.checkbox, key=lambda c: c.key) if "_sel" in (c.key or "")] == [False, True, True, False]   # QA's suggestions, ticked
    at = _click(at, "qa_7_go_TC-2")                                                             # run one: it moves to "Checked" with its result
    assert heads() == ["#### Checked (1)", "#### Partly (1)", "#### Can't be checked here (2)"]
    assert next(b for b in at.button if b.key == "qa_7_go_TC-2").label == "↻ Redo"
    assert any("TC-2" in m.value and "✅ Pass" in m.value for m in at.markdown)


def test_screen_an_inconclusive_case_can_be_redone_with_a_note(screen):
    box, open_screen = screen
    box.verdicts = {"TC-3": "inconclusive"}
    at = _start_and_run(open_screen)
    assert [c for c, _ in box.checked] == ["TC-2", "TC-3"]
    box.verdicts = {"TC-3": "pass"}
    at.text_input(key="qa_7_guide").set_value("Look at the timezone handling.").run()
    at = _click(at, "qa_7_go_TC-3")
    assert box.checked[-1] == ("TC-3", "Look at the timezone handling.") and flow.load("o", "r", 7)["results"]["TC-3"]["result"] == "pass"


def test_screen_without_a_test_plan_says_so(screen, monkeypatch):
    box, open_screen = screen
    monkeypatch.setattr(qa_plan, "runs_with_plans", lambda: [])
    at = open_screen()
    assert any("No saved run has a test plan" in i.value for i in at.info) and "▶ Start QA for this pull request" not in _labels(at)


# ---------- QA finds a defect: the coding agent fixes it, the reviewer clears it, QA tests again ----------

import subprocess

from coding import workspace as ws
from qa import fix as qa_fix
from review import loop as review_loop

BUGGY = "def average(items):\n    return sum(items[:-1]) / len(items)\n"
FIXED = "def average(items):\n    return sum(items) / len(items) if items else 0\n"
TEST = ("import unittest\nfrom calc import average\n\nclass T(unittest.TestCase):\n    def test_avg(self):\n"
        "        self.assertEqual(average([1, 2, 3]), 2)\n\nif __name__ == '__main__':\n    unittest.main()\n")


def sh(cwd, *args):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


class Coder:
    """Stands in for the coding agent: edits files in the fix worktree and answers."""

    def __init__(self, monkeypatch):
        self.calls, self.edit, self.answer = [], {"calc.py": FIXED, "test_calc.py": TEST}, {"status": "fixed", "summary": "average() dropped the last item.",
                                                                                           "explanation": "The slice was wrong.", "test_command": "python3 -m unittest"}
        monkeypatch.setattr(coding_agent, "_call", self)

    def __call__(self, label, role, prompt, system, cwd, model, resume, on_event, schema, tools, disallowed, max_turns=80):
        self.calls.append({"label": label, "role": role, "prompt": prompt, "system": system, "cwd": cwd, "resume": resume, "tools": tools, "disallowed": disallowed})
        for name, text in self.edit.items():
            (__import__("pathlib").Path(cwd) / name).write_text(text)
        return {"answer": dict(self.answer), "session_id": f"s{len(self.calls)}"}


@pytest.fixture
def fx(qa, monkeypatch, tmp_path):
    """A real remote with the merged (buggy) code, a QA record where TC-2 FAILED, a faked coding agent, and GitHub's pull requests faked."""
    remote, work = tmp_path / "gh.git", tmp_path / "w"
    sh(tmp_path, "init", "-q", "--bare", "-b", "dev", str(remote))
    sh(tmp_path, "clone", "-q", str(remote), str(work))
    (work / "calc.py").write_text(BUGGY)
    sh(work, "add", "-A"); sh(work, "commit", "-q", "-m", "merged"); sh(work, "push", "-q", "origin", "HEAD:dev")
    sha = ws.git(work, "rev-parse", "HEAD")
    monkeypatch.setattr(checkout, "ROOT", tmp_path / "rv")
    monkeypatch.setattr(qa_fix, "_remote", lambda o, n: str(remote))
    monkeypatch.setattr(review_loop, "STORE", tmp_path / "rvstore")
    s = qa.state
    s["pr"].update(merge_sha=sha, base="dev")
    s["code_sha"] = sha
    flow.run_assess(s)
    s["results"]["TC-2"] = {"result": "fail", "summary": "average([1,2,3]) is 1.0, not 2.0.", "evidence": ["calc.py:2 drops the last item"],
                            "issues": [{"severity": "blocker", "title": "Last item dropped", "detail": "items[:-1]", "where": "calc.py:2"}],
                            "commands_run": [], "at": "2020-01-01 00:00:00", "attempt": 1}
    flow.save(s)
    qa.remote, qa.work, qa.sha = remote, work, sha
    qa.coder = Coder(monkeypatch)
    qa.prs = []

    def fake_create_pr(w, title, body):
        qa.prs.append({"branch": w.branch, "title": title, "body": body})
        return {"url": f"https://github.com/o/r/pull/{100 + len(qa.prs)}", "opened": True, "note": ""}
    monkeypatch.setattr(ws, "create_pr", fake_create_pr)
    qa.status = {"state": "open", "merged": False, "merge_sha": "", "head_sha": "h", "head_ref": "b", "base_ref": "dev", "url": "u", "title": "t"}
    monkeypatch.setattr(github, "pr_status", lambda *a: dict(qa.status))
    return qa


def test_only_a_failed_test_case_can_be_fixed(fx):
    s = fx.state
    assert qa_fix.blocker(s, "TC-2") == "" and "Only a test case that failed" in qa_fix.blocker(s, "TC-3")
    s["results"]["TC-3"] = {"result": "inconclusive", "summary": "?", "evidence": [], "issues": []}
    assert "Only a test case that failed" in qa_fix.blocker(s, "TC-3")                           # doubt is not a defect to fix
    s["signoffs"]["TC-2"] = {"decision": "overridden", "by": "Dee", "note": "x"}
    assert "Take back the decision" in qa_fix.blocker(s, "TC-2")                                 # a decided test case is not worked on behind the decision
    with pytest.raises(flow.QAError):
        qa_fix.run_fix(s, "TC-2")
    assert fx.coder.calls == []


def test_the_coding_agent_works_on_a_new_branch_off_the_merged_code_and_the_tests_are_run_here(fx):
    s = fx.state
    f = qa_fix.run_fix(s, "TC-2", guidance="Keep the empty-list rule.")
    call = fx.coder.calls[0]
    for needle in ("TC-2: Verification before grant", "Submit a valid code", "Expected result: Verified first", "Last item dropped", "calc.py:2",
                   "AC-2: Submitting triggers verification.", "Keep the empty-list rule."):
        assert needle in call["prompt"], needle
    assert "ROOT CAUSE" in call["system"] and "Bash(git push:*)" in call["disallowed"] and set(call["tools"]) >= {"Read", "Edit", "Write", "Bash"}
    w = ws.Workspace.from_dict(f["ws"])
    assert w.branch == "qa-fix/pr7-tc-2" and call["cwd"] == str(w.tree) and ws.git(w.tree, "rev-parse", "HEAD") == fx.sha      # off the merged code
    assert f["status"] == "ready" and f["tests"]["report"]["passed"] == 1 and f["tests"]["report"]["failed"] == 0               # real test run, here
    assert "sum(items) / len(items) if items else 0" in qa_fix.diff(s, "TC-2") and ws.git(fx.remote, "branch", "--list") .strip().endswith("dev")
    assert "qa-fix" not in ws.git(fx.remote, "branch", "--list")                                 # nothing was pushed
    assert flow.load("o", "r", 7)["fixes"]["TC-2"][0]["status"] == "ready"                       # saved


@pytest.mark.parametrize("answer,edits,expect_outcome", [
    ({"status": "not_a_bug", "summary": "The code is right; the test case is wrong.", "explanation": "x"}, {}, "not_a_bug"),
    ({"status": "cannot_fix", "summary": "Needs a decision about trials."}, {}, "cannot_fix"),
    ({"status": "fixed", "summary": "Fixed it."}, {}, "fixed")])                                    # says fixed but changed nothing
def test_when_nothing_changed_there_is_nothing_to_approve(fx, answer, edits, expect_outcome):
    fx.coder.answer, fx.coder.edit = answer, edits
    f = qa_fix.run_fix(fx.state, "TC-2")
    assert f["status"] == "no_change" and f["outcome"] == expect_outcome and f["tests"] is None
    with pytest.raises(flow.QAError, match="no change waiting"):
        qa_fix.approve_fix(fx.state, "TC-2")
    assert ws.git(fx.remote, "branch", "--list").count("qa-fix") == 0


def test_asking_again_continues_the_same_attempt_with_the_agents_memory(fx):
    qa_fix.run_fix(fx.state, "TC-2")
    qa_fix.run_fix(fx.state, "TC-2", guidance="Also handle None.")
    assert len(fx.state["fixes"]["TC-2"]) == 1 and fx.coder.calls[1]["resume"] == "s1" and "Also handle None." in fx.coder.calls[1]["prompt"]


def test_failing_tests_block_approval_until_they_pass_or_the_person_overrides(fx):
    fx.coder.edit = {"calc.py": FIXED, "test_calc.py": TEST.replace("assertEqual(average([1, 2, 3]), 2)", "assertEqual(average([1, 2, 3]), 99)")}
    s = fx.state
    f = qa_fix.run_fix(s, "TC-2")
    assert f["tests"]["report"]["failed"] == 1 and "tests fail" in qa_fix.tests_block(f)
    with pytest.raises(flow.QAError, match="tests fail"):
        qa_fix.approve_fix(s, "TC-2")
    assert ws.git(ws.Workspace.from_dict(f["ws"]).tree, "rev-parse", "HEAD") == fx.sha and fx.prs == []         # nothing committed, pushed or opened
    with pytest.raises(flow.QAError):
        qa_fix.run_tests(s, "TC-2", "rm -rf /")
    (ws.Workspace.from_dict(f["ws"]).tree / "test_calc.py").write_text(TEST)                    # the person fixes the test by hand ...
    qa_fix.run_tests(s, "TC-2", "python3 -m unittest")                                           # ... and runs the tests again
    assert qa_fix.tests_block(qa_fix.latest(s, "TC-2")) == ""


def test_the_person_can_approve_despite_failures_that_are_not_this_changes(fx):
    fx.coder.edit = {"calc.py": FIXED, "test_calc.py": TEST.replace("2)", "99)")}
    qa_fix.run_fix(fx.state, "TC-2")
    f = qa_fix.approve_fix(fx.state, "TC-2", allow_failing_tests=True)
    assert f["status"] == "pr_open"


def test_approving_commits_pushes_the_branch_and_opens_a_pull_request_that_says_what_it_fixes(fx):
    s = fx.state
    qa_fix.run_fix(s, "TC-2")
    f = qa_fix.approve_fix(s, "TC-2", "Fix the dropped last item")
    assert f["status"] == "pr_open" and f["pr"] == {"number": 101, "url": "https://github.com/o/r/pull/101"}
    assert "qa-fix/pr7-tc-2" in ws.git(fx.remote, "branch", "--list")                            # the branch really is on the remote
    assert ws.git(fx.remote, "log", "--format=%s", "-1", "qa-fix/pr7-tc-2") == "Fix the dropped last item"
    body = fx.prs[0]["body"]
    assert fx.prs[0]["title"].startswith("Fix TC-2") and "Failed test case TC-2" in body and "Last item dropped" in body and "average() dropped the last item." in body
    assert flow.load("o", "r", 7)["fixes"]["TC-2"][0]["pr"]["number"] == 101


def test_a_pushed_branch_whose_pull_request_could_not_be_opened_can_be_retried(fx, monkeypatch):
    s = fx.state
    qa_fix.run_fix(s, "TC-2")
    monkeypatch.setattr(ws, "create_pr", lambda w, t, b: {"url": "https://github.com/o/r/compare/dev...b", "opened": False, "note": "GitHub refused"})
    with pytest.raises(flow.QAError, match="GitHub refused"):
        qa_fix.approve_fix(s, "TC-2")
    f = qa_fix.latest(s, "TC-2")
    assert f["status"] == "pushed" and "qa-fix/pr7-tc-2" in ws.git(fx.remote, "branch", "--list")
    assert "open it first" in qa_fix.blocker(s, "TC-2").lower() or "opened" in qa_fix.blocker(s, "TC-2")
    monkeypatch.setattr(ws, "create_pr", lambda w, t, b: {"url": "https://github.com/o/r/pull/55", "opened": True, "note": ""})
    assert qa_fix.open_pr(s, "TC-2")["pr"]["number"] == 55


def test_discarding_a_first_attempt_removes_it_and_the_files_it_touched(fx):
    s = fx.state
    f = qa_fix.run_fix(s, "TC-2")
    tree = ws.Workspace.from_dict(f["ws"]).tree
    assert (tree / "test_calc.py").exists()
    qa_fix.discard_fix(s, "TC-2")
    assert qa_fix.latest(s, "TC-2") is None and not (tree / "test_calc.py").exists() and (tree / "calc.py").read_text() == BUGGY
    with pytest.raises(flow.QAError):
        qa_fix.discard_fix(s, "TC-2")


# ---------- the reviewer clears it, then QA tests again ----------

def _open_pr(fx):
    qa_fix.run_fix(fx.state, "TC-2")
    qa_fix.approve_fix(fx.state, "TC-2")


def _conversation(phase, number=101):
    review_loop.STORE.mkdir(parents=True, exist_ok=True)
    (review_loop.STORE / f"o__r__{number}.json").write_text(json.dumps({"phase": phase, "repo": ["o", "r"], "number": number}))


def test_qa_tests_again_only_after_the_code_reviewer_has_cleared_the_fix(fx):
    s = fx.state
    _open_pr(fx)
    assert qa_fix.review_state(s, "TC-2") == "not_reviewed" and "Fix pull request #101 is not cleared" in qa_fix.retest_blocker(s, "TC-2")
    with pytest.raises(flow.QAError, match="TC-2: Fix pull request #101 is not cleared"):
        flow.run_checks(s, only=["TC-2"])
    _conversation("awaiting_fix")
    assert qa_fix.review_state(s, "TC-2") == "in_review" and qa_fix.retest_blocker(s, "TC-2")
    _conversation("approved")                                                                    # every review comment is settled
    assert qa_fix.review_state(s, "TC-2") == "cleared" and qa_fix.retest_blocker(s, "TC-2") == ""
    assert qa_fix.retest_blocker(fx.state, "TC-3") == ""                                         # a case without a fix is not held back


def test_the_person_may_test_before_clearance_at_their_own_risk(fx):
    _open_pr(fx)
    assert qa_fix.retest_blocker(fx.state, "TC-2", skip_review=True) == ""


def test_a_fix_pending_approval_blocks_a_retest(fx):
    qa_fix.run_fix(fx.state, "TC-2")
    assert "Approve or discard the fix" in qa_fix.retest_blocker(fx.state, "TC-2", skip_review=True)


def test_the_retest_runs_against_the_fix_pull_request_and_a_pass_marks_the_fix_verified(fx, monkeypatch):
    s = fx.state
    _open_pr(fx)
    _conversation("approved")
    seen = []
    monkeypatch.setattr(checkout, "prepare", lambda o, n, num, remote=None: (seen.append(("pr", num)), fx.work)[1])
    monkeypatch.setattr(checkout, "prepare_commit", lambda o, n, sha, remote=None: (seen.append(("sha", sha)), fx.work)[1])
    flow.run_checks(s, only=["TC-2"], guidance="Retest the empty list too.")
    assert seen == [("pr", 101)]                                                                 # the fixed code, not the buggy merged code
    r = s["results"]["TC-2"]
    assert r["result"] == "pass" and r["attempt"] == 2 and r["tested"] == {"kind": "fix_pr", "ref": "#101", "pr": 101}
    assert qa_fix.latest(s, "TC-2")["verified"] is True and flow.counts(s)["passed"] == 1


def test_a_failed_retest_starts_another_round_on_the_same_branch_and_pull_request(fx, monkeypatch):
    s = fx.state
    _open_pr(fx)
    _conversation("approved")
    fx.state["results"]["TC-2"].update(result="fail")                                            # the retest found it still broken
    fx.coder.edit = {"calc.py": FIXED + "\n# second attempt\n"}
    f2 = qa_fix.run_fix(s, "TC-2", guidance="The empty case is still wrong.")
    assert f2["n"] == 2 and f2["branch"] == "qa-fix/pr7-tc-2" and f2["pr"]["number"] == 101       # the same branch and the same pull request
    assert fx.coder.calls[-1]["cwd"] == fx.coder.calls[0]["cwd"]
    qa_fix.approve_fix(s, "TC-2", "Second attempt")
    assert len(fx.prs) == 1                                                                      # no second pull request: the first one updates itself
    assert ws.git(fx.remote, "log", "--format=%s", "-1", "qa-fix/pr7-tc-2") == "Second attempt" and qa_fix.latest(s, "TC-2")["status"] == "pr_open"


def test_discarding_a_second_attempt_leaves_the_open_pull_request_as_it_was(fx):
    s = fx.state
    _open_pr(fx)
    s["results"]["TC-2"]["result"] = "fail"
    fx.coder.edit = {"calc.py": "# nope\n"}
    qa_fix.run_fix(s, "TC-2")
    qa_fix.discard_fix(s, "TC-2")
    f = qa_fix.latest(s, "TC-2")
    assert f["n"] == 1 and f["status"] == "pr_open" and f["pr"]["number"] == 101
    assert (ws.Workspace.from_dict(f["ws"]).tree / "calc.py").read_text() == FIXED               # the first fix is still in place


def test_the_reviewers_commits_on_the_fix_branch_are_taken_in_before_the_next_attempt(fx, tmp_path):
    s = fx.state
    _open_pr(fx)
    other = tmp_path / "reviewer-side"
    sh(tmp_path, "clone", "-q", "-b", "qa-fix/pr7-tc-2", str(fx.remote), str(other))
    (other / "notes.txt").write_text("from the review loop"); sh(other, "add", "-A"); sh(other, "commit", "-q", "-m", "review fix"); sh(other, "push", "-q")
    s["results"]["TC-2"]["result"] = "fail"
    f = qa_fix.run_fix(s, "TC-2")
    assert (ws.Workspace.from_dict(f["ws"]).tree / "notes.txt").exists()                         # built on top of what the reviewer loop pushed


def test_syncing_a_merged_fix_moves_the_code_under_test_forward(fx, monkeypatch):
    s = fx.state
    _open_pr(fx)
    assert qa_fix.sync(s, "TC-2")["status"] == "pr_open"
    fx.status.update(merged=True, state="closed", merge_sha="m" * 40)
    f = qa_fix.sync(s, "TC-2")
    assert f["status"] == "merged" and f["merge_sha"] == "m" * 40 and qa_fix.code_sha(s) == "m" * 40 and qa_fix.review_state(s, "TC-2") == "merged"
    seen = []
    monkeypatch.setattr(checkout, "prepare_commit", lambda o, n, sha, remote=None: (seen.append(sha), fx.work)[1])
    assert qa_fix.tree_for(s, "TC-2")[1]["kind"] == "merged_fix" and seen == ["m" * 40]
    assert qa_fix.retest_blocker(s, "TC-2") == ""                                                # merged: no clearance question
    assert qa_fix.tree_for(s, "TC-3")[1]["kind"] == "merged" and seen[-1] == "m" * 40            # other cases are now tested on the newer code too


def test_a_closed_unmerged_fix_pull_request_is_noted_and_a_new_round_starts_a_new_branch(fx):
    s = fx.state
    _open_pr(fx)
    fx.status.update(state="closed", merged=False)
    assert qa_fix.sync(s, "TC-2")["status"] == "closed"
    s["results"]["TC-2"]["result"] = "fail"
    f = qa_fix.run_fix(s, "TC-2")
    assert f["n"] == 2 and f["branch"] == "qa-fix/pr7-tc-2-2" and f["pr"] is None                # a fresh branch off the merged code, a fresh pull request


def test_sync_needs_a_pull_request_and_reports_github_errors(fx, monkeypatch):
    with pytest.raises(flow.QAError, match="no fix pull request"):
        qa_fix.sync(fx.state, "TC-2")
    _open_pr(fx)
    monkeypatch.setattr(github, "pr_status", lambda *a: (_ for _ in ()).throw(github.GithubError("rate limited", 403)))
    with pytest.raises(flow.QAError, match="rate limited"):
        qa_fix.sync(fx.state, "TC-2")


def test_a_crashed_coding_agent_records_nothing_and_says_try_again(fx, monkeypatch):
    monkeypatch.setattr(coding_agent, "_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exit code 143")))
    with pytest.raises(flow.QAError, match="Nothing was recorded"):
        qa_fix.run_fix(fx.state, "TC-2")
    assert flow.load("o", "r", 7)["error"].startswith("The coding agent stopped while fixing TC-2")


def test_screen_a_pass_says_why_and_offers_nothing_a_failure_says_why_and_offers_two_things(screen):
    box, open_screen = screen
    box.verdicts = {"TC-2": "fail", "TC-3": "pass"}
    at = _start_and_run(open_screen)
    lines = [m.value for m in at.markdown if m.value.startswith(("Passed:", "Failed:"))]
    assert lines == ["Failed: TC-2 fail", "Passed: TC-3 pass"]                                  # the reason is the agent's own summary
    assert [b.label for b in at.button if (b.key or "").startswith(("qa_7_ovr_", "qa_7_fix_"))] == ["⚖️ Override", "🛠️ Coding agent"]    # TC-2 only
    assert not [b for b in at.button if b.key in ("qa_7_ovr_TC-3", "qa_7_fix_TC-3")]               # nothing to decide about a pass
    assert not [m for m in at.markdown if m.value == "### Results"]                                 # no separate results section


def test_screen_start_over_forgets_the_record_and_goes_back_to_the_start(screen):
    box, open_screen = screen
    at = _start_and_run(open_screen)
    assert flow.load("o", "r", 7) is not None and "↻ Redo" in _labels(at)
    at = _click(at, "qa_7_reset")
    assert not at.exception, at.exception
    assert flow.load("o", "r", 7) is None                                                         # the record is gone
    assert "▶ Start QA for this pull request" in _labels(at) and "↻ Redo" not in _labels(at)      # and the screen is back at the start
    assert any("Started over" in i.value for i in at.info)                                        # and the person is told
