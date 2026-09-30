"""POST /clusters and the join it uses. No LLM is involved anywhere in this file."""

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

import run_analysis  # noqa: E402

from app.clusters import build_clusters  # noqa: E402

from .conftest import HEADERS  # noqa: E402

DATA = PROJECT / "sample_data"
CDR = "Critical Design Review (CDR)"


def sample_rows():
    return dict(
        milestones=run_analysis.load_milestones(DATA / "milestones.xlsx"),
        tasks=run_analysis.load_csv(DATA / "asana_task_tracker_import.csv"),
        risks=run_analysis.load_csv(DATA / "asana_risk_register_import.csv"),
        engineering_notes=(DATA / "program_notes.txt").read_text(encoding="utf-8"),
    )


def dump(clusters):
    return json.dumps(clusters, indent=2, ensure_ascii=False, default=str)


def api_body(**overrides):
    body = {**sample_rows(), "as_of_date": "2026-09-18", **overrides}
    return body


# --- parity with the validated reference --------------------------------------
def test_identical_to_run_analysis_on_sample_data():
    """Same clusters, same key order, same content: what the Analyst saw in the live runs."""
    rows = sample_rows()
    for as_of in ("2026-09-18", None):
        reference = run_analysis.build_clusters(DATA, as_of)
        ours = build_clusters(
            rows["milestones"], rows["tasks"], rows["risks"], rows["engineering_notes"],
            as_of, include_empty=True,
        ).clusters
        assert dump(ours) == dump(reference)


# --- endpoint -----------------------------------------------------------------
def test_endpoint_returns_clusters_and_skips_empty_by_default(client):
    r = client.post("/clusters", json=api_body(), headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    names = [c["milestone_reference"]["Milestone"] for c in data["clusters"]]
    assert names == [
        "Authority to Operate (ATO) Approval",
        CDR,
        "Customer Demo / IOC Readiness Review",
    ]
    assert data["skipped"] == [
        {"milestone": "Preliminary Design Review (PDR)", "reason": "no linked tasks or risks"}
    ]
    assert data["warnings"] == []
    assert data["as_of_date"] == "2026-09-18"
    assert data["request_id"]
    cdr = data["clusters"][1]
    assert list(cdr)[0] == "as_of_date" and cdr["as_of_date"] == "2026-09-18"
    assert {r["Task Name"][:8] for r in cdr["risk_register"]} == {"RISK-009", "RISK-021"}
    assert client.fake.calls == []  # never touches the LLM


def test_include_empty_returns_all_four(client):
    r = client.post("/clusters", json=api_body(include_empty=True), headers=HEADERS)
    assert len(r.json()["clusters"]) == 4
    assert r.json()["skipped"] == []


def test_requires_token(client):
    assert client.post("/clusters", json=api_body()).status_code == 401
    assert client.post("/clusters", json=api_body(), headers={"X-Service-Token": "wrong"}).status_code == 401


def test_as_of_date_is_required_and_must_be_a_date(client):
    body = api_body()
    del body["as_of_date"]
    assert client.post("/clusters", json=body, headers=HEADERS).status_code == 422
    r = client.post("/clusters", json=api_body(as_of_date="next tuesday"), headers=HEADERS)
    assert r.status_code == 422


def test_no_usable_milestones_is_422(client):
    r = client.post("/clusters", json=api_body(milestones=[{"Milestone": "  "}, {"Status": "x"}]), headers=HEADERS)
    assert r.status_code == 422
    assert "Milestone" in r.json()["detail"]
    assert client.post("/clusters", json=api_body(milestones=[]), headers=HEADERS).status_code == 422


# --- join behaviour -------------------------------------------------------------
def milestone(name, status="On Track"):
    return {"Milestone": name, "Status": status}


def test_unknown_milestone_tag_warns_and_row_is_not_dropped_silently():
    res = build_clusters(
        [milestone("A")],
        [{"Task Name": "t1", "Related Milestone": "A"}, {"Task Name": "typo task", "Related Milestone": "Aa"}],
        [{"Task Name": "RISK-1", "Related Milestone": "Nope"}],
        "notes", "2026-09-18",
    )
    assert [len(c["task_tracker"]) for c in res.clusters] == [1]
    assert len(res.warnings) == 2
    assert "typo task" in res.warnings[0] and "'Aa'" in res.warnings[0]
    assert "RISK-1" in res.warnings[1]


def test_stray_whitespace_in_tags_still_matches():
    res = build_clusters(
        [milestone("A")], [{"Task Name": "t", "Related Milestone": "  A "}], [], "n", None
    )
    assert len(res.clusters[0]["task_tracker"]) == 1
    assert res.warnings == []
    # the row itself is passed through unchanged
    assert res.clusters[0]["task_tracker"][0]["Related Milestone"] == "  A "


def test_blank_rows_are_dropped_and_none_values_tolerated():
    res = build_clusters(
        [milestone("A"), {"Milestone": None}, {}],
        [{"Task Name": "", "Related Milestone": None}, {"Task Name": "t", "Related Milestone": "A", "Notes": None}],
        [{}],
        "n", None,
    )
    assert len(res.clusters) == 1
    assert len(res.clusters[0]["task_tracker"]) == 1
    assert res.clusters[0]["risk_register"] == []
    assert res.warnings == []


def test_duplicate_milestone_names_warn():
    res = build_clusters([milestone("A"), milestone("A")], [], [], "n", None, include_empty=True)
    assert any("more than once" in w for w in res.warnings)


def test_oversized_cluster_is_flagged_not_rejected():
    res = build_clusters([milestone("A")], [], [], "x" * 500, None, include_empty=True, max_chars=200)
    assert len(res.clusters) == 1
    assert any("over the 200 limit" in w for w in res.warnings)
