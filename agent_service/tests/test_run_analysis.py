"""Cluster assembly from the real sample data + the Analyst -> Executor hand-off."""

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

import run_analysis  # noqa: E402

from .conftest import ANALYST_JSON, HEADERS, j  # noqa: E402

DATA = PROJECT / "sample_data"


def by_name(clusters):
    return {c["milestone_reference"]["Milestone"]: c for c in clusters}


def test_clusters_match_source_files():
    clusters = by_name(run_analysis.build_clusters(DATA, "2026-09-18"))
    assert len(clusters) == 4
    ato = clusters["Authority to Operate (ATO) Approval"]
    cdr = clusters["Critical Design Review (CDR)"]
    demo = clusters["Customer Demo / IOC Readiness Review"]
    assert (len(ato["task_tracker"]), len(ato["risk_register"])) == (1, 1)
    assert (len(cdr["task_tracker"]), len(cdr["risk_register"])) == (3, 2)
    assert (len(demo["task_tracker"]), len(demo["risk_register"])) == (2, 0)
    pdr = clusters["Preliminary Design Review (PDR)"]
    assert not pdr["task_tracker"] and not pdr["risk_register"]
    assert cdr["milestone_reference"]["Status"] == "On Track"
    assert {r["Task Name"][:8] for r in cdr["risk_register"]} == {"RISK-009", "RISK-021"}
    assert cdr["as_of_date"] == "2026-09-18"
    assert "radar" in cdr["engineering_notes"].lower()


def test_no_as_of_matches_original_harness_shape():
    c = run_analysis.build_clusters(DATA, None)[1]
    assert list(c) == ["milestone_reference", "task_tracker", "risk_register", "engineering_notes"]


def test_analyst_then_executor_handoff(client):
    cdr = by_name(run_analysis.build_clusters(DATA, "2026-09-18"))["Critical Design Review (CDR)"]
    client.fake.replies = [j(ANALYST_JSON), "CDR is at risk. The reference needs updating."]

    def post(path, body):
        r = client.post(path, json=body, headers=HEADERS)
        return r.status_code, r.json()

    res = run_analysis.run_cluster(cdr, post)
    assert res["executor"]["output"].startswith("CDR is at risk")
    analyst_call, executor_call = client.fake.calls
    assert analyst_call["messages"][0]["content"].startswith("Source data:\n")
    assert "RISK-021" in analyst_call["messages"][0]["content"]
    assert "Critical Design Review (CDR)" in executor_call["messages"][0]["content"]
    assert ANALYST_JSON["status"] in executor_call["messages"][0]["content"]


def test_analyst_failure_is_reported_not_raised(client):
    demo = by_name(run_analysis.build_clusters(DATA, None))["Customer Demo / IOC Readiness Review"]
    client.fake.replies = ["garbage", "more garbage"]

    def post(path, body):
        r = client.post(path, json=body, headers=HEADERS)
        return r.status_code, r.json()

    res = run_analysis.run_cluster(demo, post)
    assert "analyst HTTP 502" in res["error"] and "executor" not in res
