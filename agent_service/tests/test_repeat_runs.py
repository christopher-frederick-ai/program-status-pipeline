"""scripts/repeat_runs.py: scoring, diffing, comparing, and a full run against the fake LLM."""

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

import repeat_runs as rr  # noqa: E402
import run_analysis  # noqa: E402

from .conftest import HEADERS, j  # noqa: E402

ATO = "Authority to Operate (ATO) Approval"
CDR = "Critical Design Review (CDR)"
DEMO = "Customer Demo / IOC Readiness Review"


def rec(flag="no", status="ATO is at risk. The forecast has slipped two weeks.", explanation="Evidence supports the label.",
        removal=None, executor=None, attempts=1):
    out = {
        "analyst": {
            "confidence_score": 4,
            "milestone_status_flagged": flag,
            "status": status,
            "explanation": explanation,
            "risks_recommended_for_removal": removal or [],
        },
        "attempts": attempts,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "model": "fake-model",
        "seconds": 1.0,
    }
    if executor is not None:
        out["executor_text"] = executor
    return out


def failing(check, r, name=ATO, with_executor=False):
    result = rr.evaluate(r, rr.expectation_for(name), with_executor)
    return [c for c, ok in result.items() if not ok] == [check]


# --- scoring -----------------------------------------------------------------------
def test_sentence_split_ignores_initials():
    assert len(rr.sentences("R. Chen owns the task. It is late.")) == 2
    assert len(rr.sentences("One. Two. Three.")) == 3
    assert rr.sentences("") == []


def test_good_run_passes_every_check():
    checks = rr.evaluate(rec(), rr.expectation_for(ATO), with_executor=False)
    assert checks and all(checks.values())


def test_each_check_can_fail_on_its_own():
    assert failing("flag matches expected", rec(flag="yes"))
    assert failing("removal list matches expected", rec(removal=["RISK-021"]))
    assert failing("status is 1-2 sentences", rec(status="One. Two. Three."))
    assert failing("neutral wording (no banned adjectives)", rec(status="ATO has a comfortable runway."))
    assert failing("valid JSON on first attempt", rec(attempts=2))


def test_banned_word_in_explanation_also_counts_and_is_whole_word():
    assert not rr.evaluate(rec(explanation="A healthy margin."), None, False)["neutral wording (no banned adjectives)"]
    assert rr.evaluate(rec(explanation="The unhealthy trend, soliders."), None, False)["neutral wording (no banned adjectives)"]


def test_removal_ids_accept_strings_or_objects():
    r = rec(flag="yes", removal=[{"risk_id": "RISK-021", "title": "t", "reason": "r"}])
    assert rr.evaluate(r, rr.expectation_for(CDR), False)["removal list matches expected"]
    r = rec(flag="yes", removal=["RISK-021"])
    assert rr.evaluate(r, rr.expectation_for(CDR), False)["removal list matches expected"]


def test_resource_conflict_must_be_mentioned_by_analyst_and_executor():
    exp = rr.expectation_for(DEMO)
    good = rec(explanation="R. Chen is behind on CDR and owns the dry-run task.", executor="Demo is on track. R. Chen is stretched.")
    checks = rr.evaluate(good, exp, with_executor=True)
    assert all(checks.values())
    missing = rec(explanation="No issues.", executor="Demo is on track.")
    checks = rr.evaluate(missing, exp, with_executor=True)
    assert not checks["analyst mentions R. Chen"] and not checks["executor mentions R. Chen"]


def test_executor_checks_semicolons_and_length():
    exp = rr.expectation_for(ATO)
    assert failing("executor: no semicolons", rec(executor="ATO is late; it slipped."), with_executor=True)
    assert failing("executor: 1-3 sentences", rec(executor="ATO is late. It slipped. Review is needed. Act now."), with_executor=True)
    no_text = rr.evaluate(rec(executor=None), exp, True)  # a missing Executor paragraph fails its checks
    assert not no_text["executor: no semicolons"] and not no_text["executor: 1-3 sentences"]


def test_ato_report_must_not_claim_a_missing_formal_risk():
    exp = rr.expectation_for(ATO)
    bad = rec(executor="ATO is at risk. The informal slip estimate has not yet been reflected as a formal risk update.")
    assert failing("executor avoids 'formal risk'", bad, with_executor=True)
    assert rr.evaluate(rec(executor="ATO is at risk."), exp, True)["executor avoids 'formal risk'"]
    assert not rr.evaluate(rec(executor=None), exp, True)["executor avoids 'formal risk'"]  # no paragraph, no pass


def test_failed_run_fails_everything():
    checks = rr.evaluate({"error": "analyst HTTP 502: x"}, rr.expectation_for(ATO), True)
    assert checks and not any(checks.values())


# --- comparing text -----------------------------------------------------------------
def test_word_diff_marks_changes_and_shortens_long_unchanged_stretches():
    assert rr.word_diff("a b c", "a b c") == "a b c"
    assert rr.word_diff("has a comfortable runway", "has a limited runway") == "has a [-comfortable-] {+limited+} runway"
    long_same = " ".join(f"w{i}" for i in range(40))
    d = rr.word_diff(long_same + " old", long_same + " new")
    assert "words unchanged" in d and d.endswith("[-old-] {+new+}")


def test_similarity_and_consistency():
    assert rr.similarity("a b c", "a b c") == 1.0
    assert 0 < rr.similarity("a b c d", "a b x d") < 1
    assert rr.consistency(["only one"]) is None
    assert rr.consistency(["a b", "a b", "a b"]) == 1.0
    assert rr.medoid(["a b c", "a b c", "x y z"]) in (0, 1)


# --- reports ------------------------------------------------------------------------
def dataset(label, records, prompts=None, with_executor=False):
    return {
        "meta": {"label": label, "created": "2026-09-21T10:00:00", "runs": len(records), "as_of": "2026-09-18",
                 "with_executor": with_executor, "model": "fake-model", "prompts": prompts or {"analyst": "aaaa1111", "executor": "bbbb2222"}},
        "milestones": {ATO: records},
    }


def test_report_shows_checks_variation_and_diff():
    data = dataset("t", [rec(status="ATO is at risk. Forecast slipped two weeks."),
                         rec(status="ATO is at risk. Forecast slipped about two weeks.", flag="yes")])
    text = rr.build_report(data)
    assert "=== Authority to Operate (ATO) Approval   (2 runs, 0 failed) ===" in text
    assert "1/2  flag matches expected   <-- not every run" in text
    assert "2/2  run completed" in text
    assert "flag: no x1, yes x1" in text
    assert "{+about+}" in text  # the most-different-pair diff
    assert "Summary:" in text and "analyst aaaa1111" in text


def test_identical_runs_report_no_diff():
    text = rr.build_report(dataset("t", [rec(), rec()]))
    assert "status: identical in every run" in text


def test_compare_flags_improvements_and_regressions_and_notes():
    before = dataset("before", [rec(flag="yes"), rec(flag="yes")], prompts={"analyst": "old11111", "executor": "x"})
    after = dataset("after", [rec(), rec(status="Too. Many. Sentences.")], prompts={"analyst": "new22222", "executor": "x"})
    text = rr.build_compare(before, after)
    assert "flag matches expected" in text and "better" in text
    assert "status is 1-2 sentences" in text and "WORSE" in text
    assert "1 check(s) better, 1 worse" in text
    same = rr.build_compare(before, before)
    assert "same prompt fingerprints" in same
    after["meta"]["as_of"] = "2026-09-21"
    assert "different as_of_date" in rr.build_compare(before, after)


# --- running against the fake LLM ---------------------------------------------------------
class Api:
    """post(path, body) -> (status, json) over the test client, like smoke_test.call."""

    def __init__(self, client):
        self.client = client

    def __call__(self, path, body):
        r = self.client.post(path, json=body, headers=HEADERS)
        return r.status_code, r.json()


def cdr_cluster():
    clusters = run_analysis.build_clusters(PROJECT / "sample_data", "2026-09-18")
    return next(c for c in clusters if c["milestone_reference"]["Milestone"] == CDR)


ANALYST_A = {"confidence_score": 3, "milestone_status_flagged": "yes", "status": "CDR is behind. The radar task is overdue.",
             "explanation": "On Track is not supported.", "risks_recommended_for_removal": [{"risk_id": "RISK-021", "title": "t", "reason": "r"}]}


def test_collect_runs_each_milestone_n_times_and_records_results(client):
    client.fake.replies = [j(ANALYST_A), "CDR is behind. The radar task is overdue.",
                           j({**ANALYST_A, "confidence_score": 4}), "CDR is behind. RISK-021 can be removed."]
    msgs = []
    out = rr.collect([cdr_cluster()], 2, Api(client), True, 1, msgs.append)
    recs = out[CDR]
    assert [r["analyst"]["confidence_score"] for r in recs] == [3, 4]
    assert recs[1]["executor_text"].endswith("removed.")
    assert all("error" not in r and r["attempts"] == 1 for r in recs)
    assert len(msgs) == 2 and msgs[0].startswith("1/2 CDR run 1")
    assert len(client.fake.calls) == 4


def test_a_failed_call_becomes_a_record_not_a_crash(client):
    client.fake.replies = ["garbage", "more garbage"]
    r = rr.run_once(cdr_cluster(), Api(client), True)
    assert "analyst HTTP 502" in r["error"] and "analyst" not in r

    def boom(path, body):
        raise ConnectionRefusedError("nope")

    assert "ConnectionRefusedError" in rr.run_once(cdr_cluster(), boom, True)["error"]


def test_main_runs_saves_and_can_report_and_compare(client, monkeypatch, tmp_path, capsys):
    client.fake.replies = [j(ANALYST_A), "CDR is behind.", j(ANALYST_A), "CDR is behind."]

    def fake_call(method, path, secret=None, body=None):
        r = client.request(method, path, json=body, headers=HEADERS)
        return r.status_code, r.json()

    monkeypatch.setattr(rr.smoke_test, "call", fake_call)
    monkeypatch.setattr(rr.smoke_test, "read_secret", lambda: "s")
    out = tmp_path / "set1.json"
    monkeypatch.setattr(sys, "argv", ["repeat_runs.py", "--runs", "2", "--milestone", "Critical Design", "--workers", "1",
                                      "--label", "first try", "--as-of", "2026-09-18", "--out", str(out)])
    rr.main()
    shown = capsys.readouterr().out
    assert "Instruction files re-read by the service (HTTP 200)" in shown
    assert "1 milestone(s) [CDR] x 2 runs = 4 API call(s)" in shown
    assert "Summary:" in shown and "Saved" in shown

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["meta"]["label"] == "first try" and saved["meta"]["runs"] == 2
    assert len(saved["milestones"][CDR]) == 2
    assert out.with_suffix(".txt").read_text(encoding="utf-8").startswith("Run set: first try")

    monkeypatch.setattr(sys, "argv", ["repeat_runs.py", "--report", str(out)])
    rr.main()
    assert "=== Critical Design Review (CDR)" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["repeat_runs.py", "--compare", str(out), str(out)])
    rr.main()
    assert "Overall: 0 check(s) better, 0 worse." in capsys.readouterr().out


def test_dry_run_calls_nothing(monkeypatch, capsys):
    monkeypatch.setattr(rr.smoke_test, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no calls in dry run")))
    monkeypatch.setattr(sys, "argv", ["repeat_runs.py", "--dry-run", "--runs", "5"])
    rr.main()
    shown = capsys.readouterr().out
    assert "3 milestone(s) [ATO, CDR, Customer Demo] x 5 runs = 30 API call(s)" in shown
    assert "Prompt fingerprints" in shown
