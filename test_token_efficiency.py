"""Tests for the token-saving features that remain: the token ledger, no wasted retries, and the knowledge-base page
being shown first (so the AI service can cache it)."""

import json
from pathlib import Path

import pytest

import config
import knowledge_base.loader as kb
import llm
import test_hive as t
import token_log
from agents import design as design_agent


# ---------- token ledger ----------

def test_the_token_ledger_records_events_and_never_raises(monkeypatch):
    token_log.record("probe", "Tester", n=1)
    line = json.loads((config.RUNS_ROOT / "token_log.jsonl").read_text().splitlines()[-1])
    assert line["event"] == "probe" and line["agent"] == "Tester" and line["n"] == 1
    monkeypatch.setattr(config, "RUNS_ROOT", Path("/proc/definitely/not/writable"))
    token_log.record("probe", "Tester")                                      # swallowed







# ---------- no wasted retries, per-attempt ledger ----------

import llm
import test_hive as t


def _count_calls(monkeypatch, outcomes):
    calls = []

    async def fake_call_once(prompt, system, schema, model=None):
        calls.append(prompt)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(llm, "_call_once", fake_call_once)
    return calls


def test_an_error_no_retry_can_fix_stops_at_once(monkeypatch):
    calls = _count_calls(monkeypatch, [Exception("You've hit your session limit · resets 7:20pm")] * 3)
    with pytest.raises(ValueError, match="after 1 attempt:"):
        llm.call_llm_json("p", "Design Agent", required_keys=["a"])
    assert len(calls) == 1


def test_our_own_content_rejections_are_always_retried_whatever_words_they_use(monkeypatch):
    calls = _count_calls(monkeypatch, [{"a": 1}, {"a": 2}])

    def check(r):
        if r["a"] == 1:
            raise ValueError("authentication-service is listed twice; quota-service is missing")
    out = llm.call_llm_json("p", "Design Agent", required_keys=["a"], validate=check)
    assert out == {"a": 2} and len(calls) == 2


def test_every_attempt_is_written_to_the_ledger_including_the_start(monkeypatch):
    _count_calls(monkeypatch, [Exception("boom"), {"a": 1}])
    llm.call_llm_json("p", "Ledger Agent", required_keys=["a"])
    events = [json.loads(l) for l in (config.RUNS_ROOT / "token_log.jsonl").read_text().splitlines()]
    mine = [e["event"] + (":ok" if e.get("ok") else "") for e in events if e["agent"] == "Ledger Agent"]
    assert mine[0] == "llm_start" and mine.count("llm_attempt") == 1 and "llm_attempt:ok" in mine




# ---------- the knowledge base is one page, shown first ----------

def _doc(name):
    return kb.ApplicationDoc(name, Path(name), f"# {name}\n\nBody of {name}.", None, None)


def test_the_knowledge_base_comes_before_the_requirement_so_it_can_be_cached(monkeypatch):
    prompts = []

    def fake(prompt, agent_name, **kw):
        prompts.append(prompt)
        return {"affected_applications": []}
    monkeypatch.setattr(design_agent.llm, "call_llm_json", fake)
    for fn in ("_validate", "_clean_architecture", "_clean_kb_updates"):
        monkeypatch.setattr(design_agent, fn, lambda *a, **k: None)
    monkeypatch.setattr(design_agent, "validate_clarifying_questions", lambda *a, **k: None)
    design_agent.run({"title": "Let customers redeem points"}, [_doc("loyalty"), _doc("billing")])
    p = prompts[0]
    assert p.index("KNOWLEDGE BASE") < p.index("REQUIREMENT") < p.index("OUTPUT")
    assert "Body of loyalty" in p and "Body of billing" in p                 # nothing is left out of the page


def test_a_knowledge_base_file_is_shown_exactly_as_written(tmp_path):
    page = tmp_path / "shop.md"
    page.write_text("# shop\n\nWhat it is.\n\n## Applications\n\n- `billing` — invoices and payments\n- `tax` — tax rates\n")
    docs = kb.load_all(page)
    assert [d.name for d in docs] == ["billing", "tax"] and docs[0].purpose == "invoices and payments"
    assert kb.kb_page(docs) == page.read_text().strip()


# ---------- Phase 1: overview for Requirement, cached parsing, spelling-tolerant names ----------

def test_the_requirement_stage_gets_the_overview_not_the_whole_page(monkeypatch):
    from agents import requirement as requirement_agent
    prompts = []

    def fake(prompt, agent_name, **kw):
        prompts.append(prompt)
        return {}
    monkeypatch.setattr(requirement_agent.llm, "call_llm_json", fake)
    monkeypatch.setattr(requirement_agent, "validate_clarifying_questions", lambda *a, **k: None)
    long_doc = kb.ApplicationDoc("billing", Path("billing"), "# billing\n\n" + "Long detail. " * 500, None, "invoices")
    try:
        requirement_agent.run("add refunds", [long_doc])
    except Exception:
        pass                                              # only the prompt matters here
    assert "- **billing**: invoices" in prompts[0] and "Long detail." not in prompts[0]


def test_a_page_is_parsed_once_until_it_changes(tmp_path, monkeypatch):
    page = tmp_path / "shop.md"
    page.write_text("# shop\n\n## Applications\n\n- `billing` — invoices\n")
    calls = []
    real = kb._parse
    monkeypatch.setattr(kb, "_parse", lambda p: calls.append(p) or real(p))
    kb.load_all(page), kb.load_all(page), kb.list_knowledge_bases(tmp_path)
    assert len(calls) == 1
    kb.create_doc("tax", "Tax rates.", page)                    # a write changes the file ...
    assert [d.name for d in kb.load_all(page)] == ["billing", "tax"]    # ... so it is read again


def test_application_names_match_whatever_the_spelling(tmp_path):
    page = tmp_path / "d2c_user.md"
    page.write_text("# D2C user\n\nUser-data backend and OAuth2 login server for the streaming app.\n")
    assert kb.canonical_name("D2C-User", ["d2c_user"]) == "d2c_user"
    assert kb.canonical_name("billing", ["d2c_user"]) is None
    out = kb.apply_updates([{"application": "d2c_user", "action": "create", "content": "More."}], kb_dir=page)
    assert out[0]["action"] == "augment" and out[0]["status"] == "applied"      # not a second, duplicate application
    assert page.read_text().count("## Update to d2c_user") == 1


def test_design_answers_use_the_known_spelling(monkeypatch):
    result = {"affected_applications": [{"name": "D2C-User"}], "dependency_order": ["d2c-user"],
              "components": [{"name": "d2c-user"}], "knowledge_base_updates": [{"application": "d2c-user"}],
              "overview": "The d2c-user service stays as it is."}
    design_agent._use_known_spellings(result, ["d2c_user"])
    assert result["affected_applications"][0]["name"] == "d2c_user" and result["dependency_order"] == ["d2c_user"]
    assert result["components"][0]["name"] == "d2c_user" and result["knowledge_base_updates"][0]["application"] == "d2c_user"
    assert result["overview"] == "The d2c-user service stays as it is."          # prose is left alone
