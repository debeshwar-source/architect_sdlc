"""Tests for the ledger report and the replay comparison."""

import json

import config
import llm
import replay
import token_report


def _rows():
    return [
        {"ts": "2026-10-15T06:00:00+00:00", "event": "llm_attempt", "agent": "Design Agent", "attempt": 1, "ok": False,
         "error": "components is empty: list every service"},
        {"ts": "2026-10-15T06:01:00+00:00", "event": "llm_call", "agent": "Design Agent", "ok": True, "attempts": 2,
         "answer_tokens_est": 4000, "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3000,
                                              "cache_read_input_tokens": 21000, "output_tokens": 16000},
         "stats": {"turns": 2, "thinking_tokens_est": 9000, "cost_usd": 0.31}},
        {"ts": "2026-10-14T06:01:00+00:00", "event": "llm_call", "agent": "Old Agent", "ok": True, "attempts": 1,
         "usage": {"output_tokens": 100}},
    ]


def test_the_report_separates_the_answer_from_hidden_output_and_shows_cache_use():
    s = token_report.summarise(_rows())["Design Agent"]
    assert s["hidden_output_share"] == 0.75 and s["cache_hit_rate"] == 0.87 and s["turns_avg"] == 2.0
    assert s["failed_attempts"] == 1 and "components is empty" in s["rejection_reasons"][0]
    text = token_report.render(token_report.summarise(_rows()))
    assert "hidden 75%" in text and "cache hit 87%" in text


def test_old_ledger_rows_without_the_new_fields_do_not_distort_the_report():
    s = token_report.summarise(_rows())["Old Agent"]
    assert s["hidden_output_share"] is None and s["turns_avg"] is None


def test_the_report_can_filter_by_time_and_agent(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _rows()))
    assert {r["agent"] for r in token_report.load(since="2026-10-15", path=p)} == {"Design Agent"}
    assert {r["agent"] for r in token_report.load(agent="old", path=p)} == {"Old Agent"}


def test_compare_scores_identical_answers_as_1_and_changed_ones_lower():
    d = {"change_required": True, "affected_applications": [{"name": "a"}], "new_applications_needed": [{"name": "n"}],
         "components": [{"name": "a"}, {"name": "n"}], "connections": [{"from": "a", "to": "n"}], "flows": []}
    assert replay.compare("design", d, d)["similarity"] == 1.0
    changed = dict(d, components=[{"name": "a"}], connections=[])
    r = replay.compare("design", d, changed)
    assert r["similarity"] < 1.0 and r["parts"]["components"] == 0.5 and r["parts"]["connections"] == 0.0


def test_requirement_compare_notices_a_dropped_acceptance_criterion():
    old = {"acceptance_criteria": ["Users can redeem a valid coupon for time-limited access", "Expired coupons are rejected"],
           "build_mode": "extend_existing", "scope_summary": "coupon redemption"}
    new = dict(old, acceptance_criteria=["Users can redeem a valid coupon for time-limited access"])
    assert replay.compare("requirement", old, new)["parts"]["criteria_covered"] == 0.5
    assert replay.compare("requirement", old, old)["similarity"] == 1.0


def test_quality_and_release_compare_run():
    q = {"test_cases": [{"component": "x", "requirement_refs": ["AC-1"]}]}
    assert replay.compare("quality", q, q)["similarity"] == 1.0
    r = {"stories": [{"component": "x", "tasks": [{"assignment": {"agent": "coding_python"}}]}]}
    assert replay.compare("release", r, r)["similarity"] == 1.0


def test_without_reading_tools_the_ai_call_options_are_just_the_turn_limit():
    assert llm._options_kwargs() == {"max_turns": llm.MAX_TURNS}
