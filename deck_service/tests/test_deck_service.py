import json
from pathlib import Path

import pytest

from conftest import ROOT, asset, fake_imager, fake_renderer

H = {"X-Service-Token": "s3cret"}


def post(api, payload, headers=H):
    return api.post("/render_weekly_deck", json=payload, headers=headers)


# ---- happy path ------------------------------------------------------------
def test_success_first_try(api, client, payload, settings):
    client.messages.queue("good")
    r = post(api, payload)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "ok" and b["failed_checks"] == [] and b["repair_rounds"] == 0
    assert b["slide_count"] == 7 and len(b["checks"]) == 21
    assert b["usage"] == {"input_tokens": 10, "output_tokens": 5}
    for k in ("pptx", "pdf", "manifest"):
        assert Path(b["files"][k]).exists() and str(settings.output_dir) in b["files"][k]
    # request shape sent to the model
    assert len(client.messages.calls) == 1
    kw = client.messages.calls[0]
    assert kw["tools"] == [{"type": "code_execution_20250825", "name": "code_execution"}]
    assert kw["container"]["skills"] == [{"type": "anthropic", "skill_id": "pptx", "version": "latest"}]
    assert "Deck Builder" in kw["system"]
    assert "2026-09-24" in kw["messages"][0]["content"] and "options" not in kw["messages"][0]["content"]


def test_only_wanted_files_are_taken(api, client, payload):
    client.messages.queue("good", extra_files={"scratch.txt": b"x", "deck.pdf": b"model's own pdf"})
    b = post(api, payload).json()
    assert b["status"] == "ok"
    assert Path(b["files"]["pdf"]).read_bytes() == asset("good", "pdf")  # our render, not the model's


# ---- repair loop -----------------------------------------------------------
def test_repair_then_success(api, client, payload):
    client.messages.queue("bad"); client.messages.queue("good")
    b = post(api, payload).json()
    assert b["status"] == "ok" and b["repair_rounds"] == 1
    assert b["usage"] == {"input_tokens": 20, "output_tokens": 10}
    second = client.messages.calls[1]
    assert second["container"]["id"] == "cont_1"                     # same sandbox reused
    last = second["messages"][-1]
    assert last["role"] == "user"
    text = last["content"][0]["text"]
    assert "removal_risk_ids_shown" in text and "no_overlapping_text" in text
    assert any(c["type"] == "image" for c in last["content"])        # page images attached
    assert second["messages"][-2]["role"] == "assistant"             # prior turn carried forward


def test_repair_exhausted_returns_deck_marked_needs_review(api, client, payload, settings):
    settings.max_repairs = 1
    client.messages.queue("bad"); client.messages.queue("bad")
    b = post(api, payload).json()
    assert b["status"] == "needs_review" and b["repair_rounds"] == 1
    assert {"no_overlapping_text", "no_false_all_clear", "removal_risk_ids_shown"} <= set(b["failed_checks"])
    assert Path(b["files"]["pptx"]).exists()
    assert len(client.messages.calls) == 2                           # never loops past the cap


def test_best_round_kept_when_repair_loses_files(api, client, payload):
    client.messages.queue("bad"); client.messages.queue(None); client.messages.queue(None)
    b = post(api, payload).json()
    assert b["status"] == "needs_review" and Path(b["files"]["pptx"]).exists()


def test_model_never_saves_files_is_502(api, client, payload):
    for _ in range(3):
        client.messages.queue(None)
    r = post(api, payload)
    assert r.status_code == 502 and "never saved" in r.json()["detail"]


def test_pause_turn_is_followed(api, client, payload, settings):
    settings.max_pauses = 1
    client.messages.queue(None, stop="pause_turn"); client.messages.queue("good")
    b = post(api, payload).json()
    assert b["status"] == "ok" and len(client.messages.calls) == 2
    assert client.messages.calls[1]["messages"][-1]["role"] == "assistant"


# ---- auth, validation, errors ----------------------------------------------
def test_auth_required(api, payload):
    assert post(api, payload, headers={}).status_code == 401
    assert post(api, payload, headers={"X-Service-Token": "wrong"}).status_code == 401


def test_fails_closed_without_configured_token(client, payload, settings):
    from fastapi.testclient import TestClient
    from deck_service.app import create_app
    settings.service_token = ""
    api = TestClient(create_app(client_factory=lambda: client, settings=settings))
    assert post(api, payload).status_code == 503


@pytest.mark.parametrize("mutate", [
    lambda p: p["sidecar"].update(date="Sept 24"),
    lambda p: p["sidecar"].update(milestones={}),
    lambda p: p["sidecar"]["milestones"]["CDR"].update(milestone_status_flagged="maybe"),
    lambda p: p["milestone_rows"][1].update({"Planned Date": "next week"}),
    lambda p: p["milestone_rows"][1].update({"Forecast/Actual Date": "soon"}),
    lambda p: p.update(milestone_rows=[]),
    lambda p: p["sidecar"]["milestones"]["CDR"].update(name="Critical Design Review (CDR) v2"),  # adapter mismatch
    lambda p: p["milestone_rows"].append(dict(p["milestone_rows"][0])),                          # duplicate row
])
def test_bad_input_is_422(api, payload, mutate):
    mutate(payload)
    assert post(api, payload).status_code == 422


def test_dates_as_excel_datetimes_and_flag_case_accepted(api, client, payload):
    payload["sidecar"]["milestones"]["CDR"]["milestone_status_flagged"] = "YES"
    payload["milestone_rows"][1]["Planned Date"] = "2026-09-30T00:00:00.000Z"
    payload["milestone_rows"][3]["Forecast/Actual Date"] = "tbd"
    client.messages.queue("good")
    assert post(api, payload).status_code == 200


def test_model_error_does_not_leak_content(api, client, payload):
    client.messages.script.append(RuntimeError("SECRET PROGRAM DETAIL"))
    r = post(api, payload)
    assert r.status_code == 502 and "SECRET" not in r.text and "RuntimeError" in r.text


def test_health(api):
    b = api.get("/healthz").json()
    assert b["ok"] and b["instructions_present"]


# ---- checker used standalone -----------------------------------------------
def test_checker_good_and_bad_assets(payload):
    from deck_service.adapter import build_deck_input
    from deck_service.checks import normalize_input, run_checks
    from deck_service.schemas import DeckRequest
    import tempfile
    inp = normalize_input(build_deck_input(DeckRequest(**payload)))
    expected = {"good": set(), "bad": {"accuracy_flags_match_input", "removal_risk_ids_shown", "no_unknown_dates",
                                       "no_false_all_clear", "no_overlapping_text"}}
    for name, want in expected.items():
        d = Path(tempfile.mkdtemp())
        for ext, fn in (("pptx", "deck.pptx"), ("pdf", "deck.pdf"), ("json", "manifest.json")):
            (d / fn).write_bytes(asset(name, ext))
        results = run_checks(inp, d)
        assert len(results) == 21 and {r[0] for r in results if not r[1]} == want, name


# ---- adapter on the real sidecars --------------------------------------------
import glob


@pytest.mark.parametrize("path", sorted(glob.glob(str(ROOT / "fixtures" / "week_2026-*.json"))))
def test_adapter_on_real_weeks(path):
    from deck_service.adapter import build_deck_input
    from deck_service.schemas import DeckRequest
    fx = json.loads(Path(path).read_text()); fx.pop("_comment", None)
    inp = build_deck_input(DeckRequest(**fx))
    assert inp["program"] == "Meridian Defense Systems C2 Platform"
    assert [m["id"] for m in inp["milestones"]] == ["PDR", "ATO", "CDR", "DEMO"]
    pdr = inp["milestones"][0]
    assert pdr["in_weekly_report"] is False and "approved_text" not in pdr
    for m in inp["milestones"][1:]:
        assert m["in_weekly_report"] and m["approved_text"]
        assert "explanation" not in m and "status" not in m and "analyst" not in m   # Analyst notes withheld
        assert isinstance(m["reported_status_supported"], bool)


def test_sidecar_wrapped_in_data_is_accepted(payload):
    from deck_service.adapter import build_deck_input
    from deck_service.schemas import DeckRequest
    payload["sidecar"] = {"data": payload["sidecar"]}
    assert build_deck_input(DeckRequest(**payload))["report_date"] == "2026-09-24"


def test_adapter_flag_is_inverted_to_supported(payload):
    from deck_service.adapter import build_deck_input
    from deck_service.schemas import DeckRequest
    ms = {m["id"]: m for m in build_deck_input(DeckRequest(**payload))["milestones"]}
    assert ms["CDR"]["reported_status_supported"] is False   # flagged "yes" = NOT supported
    assert ms["ATO"]["reported_status_supported"] is True and ms["DEMO"]["reported_status_supported"] is True
    assert ms["CDR"]["risks_recommended_for_removal"] == [
        {"risk_id": "RISK-021", "title": "Subcontractor staffing gap on fusion engine team"}]


def test_adapter_older_sidecar_without_titles_and_light_week(payload):
    from deck_service.adapter import build_deck_input
    from deck_service.schemas import DeckRequest
    for k in ("ATO", "DEMO"):
        del payload["sidecar"]["milestones"][k]
    for k in ("slide_title", "slide_subtitle", "report", "subject"):
        payload["sidecar"]["milestones"]["CDR"].pop(k, None); payload["sidecar"].pop(k, None)
    inp = build_deck_input(DeckRequest(**payload))
    assert [m["in_weekly_report"] for m in inp["milestones"]] == [False, False, True, False]
    assert inp["milestones"][2]["slide_title"] == ""


# ---- repeat-run tooling ----------------------------------------------------
def test_run_many_and_summary(client, payload, settings):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from repeat_deck_runs import run_many, summarize
    from deck_service.schemas import DeckRequest
    from deck_service.adapter import build_deck_input
    for d in ("good", "bad", "good", "bad", "bad"):   # with max_repairs=0, three of five end needing review
        client.messages.queue(d)
    settings.max_repairs = 0
    runs = run_many(build_deck_input(DeckRequest(**payload)), 5, client, settings, renderer=fake_renderer, imager=fake_imager)
    s = summarize(runs)
    assert s["runs"] == 5 and s["all_checks_passed"] == 2 and s["slide_counts"] == [(7, 5)]
    assert s["checks_failing_in_final_decks"]["no_overlapping_text"] == 3


# ---- n8n workflow files (generated by n8n/build_workflow3.py, committed under n8n/) -------------
N8N = ROOT.parent / "n8n"
WORKFLOWS = [("workflow-3-deck-review.json", "hybrid"),
             ("alternatives/workflow-3-slack-buttons.json", "slack"),
             ("alternatives/workflow-3-gmail-only.json", "gmail")]


@pytest.mark.parametrize("fname,kind", WORKFLOWS)
def test_workflow3_json_is_wired_and_clean(fname, kind):
    import re
    raw = (N8N / fname).read_text()
    w = json.loads(raw)
    names = {n["name"] for n in w["nodes"]}
    assert len(names) == len(w["nodes"])
    targets = {l["node"] for c in w["connections"].values() for outs in c["main"] for l in outs}
    assert targets <= names and set(w["connections"]) <= names
    assert names - targets == {"When Executed by Another Workflow", "Manual run"}
    for ref in re.findall(r"\$\('([^']+)'\)", raw):
        assert ref in names, ref
    assert not re.search(r"sk-ant|xoxb|Bearer ", raw)
    assert w["active"] is False
    assert any("slack" in n["type"] for n in w["nodes"]) == (kind != "gmail")
    for n in w["nodes"]:
        for k in ("message", "text"):
            v = n["parameters"].get(k)
            assert v is None or "\\n" not in v, (n["name"], k)           # no literal backslash-n in message text
    appr = next(n for n in w["nodes"] if n["name"] == "Request deck approval")
    assert appr["parameters"]["operation"] == "sendAndWait"
    assert appr["type"].endswith(".slack" if kind == "slack" else ".gmail")
    if kind == "slack":
        assert appr["parameters"]["approvers"] == ["YOUR_SLACK_USER_ID"]
    if kind == "hybrid":
        # Slack notices stay; only the approval click moves to Gmail; a Slack heads-up sits before it
        assert w["connections"]["Email draft to reviewer"]["main"][0][0]["node"] == "Slack: draft ready"
        assert w["connections"]["Slack: draft ready"]["main"][0][0]["node"] == "Request deck approval"
        assert next(n for n in w["nodes"] if n["name"] == "Notify build failed")["type"].endswith(".slack")


@pytest.mark.parametrize("fname,kind", WORKFLOWS)
def test_published_workflows_hold_no_credentials_or_personal_ids(fname, kind):
    """The committed exports must be safe to publish: no credential references, no real IDs or addresses."""
    import re
    raw = (N8N / fname).read_text()
    w = json.loads(raw)
    assert all("credentials" not in n for n in w["nodes"])
    assert "YOUR_SLACK_USER_ID" in raw and "YOUR_EMAIL@example.com" in raw
    assert not re.search(r"\bU[A-Z0-9]{8,}\b", raw.replace("YOUR_SLACK_USER_ID", ""))
    assert not re.search(r"[\w.+-]+@(?!example\.com)[\w-]+\.[\w.]+", raw)


def test_w1_calls_the_deck_workflow_with_a_placeholder_id():
    w = json.loads((N8N / "workflow-1-project-status-reporting.json").read_text())
    call = next(n for n in w["nodes"] if n["name"] == "Call 'Slide Build'")
    ref = call["parameters"]["workflowId"]
    assert ref["cachedResultName"] == "Deck Review and Distribute"
    assert ref["value"] == "YOUR_DECK_REVIEW_WORKFLOW_ID"


# ---- call cap and per-call logging --------------------------------------------
def test_call_cap_stops_runaway_loop(api, client, payload, settings):
    settings.max_calls = 3; settings.max_pauses = 10
    for _ in range(5):
        client.messages.queue(None, stop="pause_turn")
    r = post(api, payload)
    assert r.status_code == 502 and "model calls" in r.json()["detail"]
    assert len(client.messages.calls) == 3


def test_per_call_log_has_counts_not_content(api, client, payload, caplog):
    import logging
    caplog.set_level(logging.INFO)
    client.messages.queue("bad"); client.messages.queue("good")
    post(api, payload)
    lines = [r.getMessage() for r in caplog.records if "model call" in r.getMessage()][:1]
    rounds = [r.getMessage() for r in caplog.records if "deck build request" in r.getMessage()]
    assert "failed_names=accuracy_flags_match_input" in rounds[0] and "failed_names=none" in rounds[1]
    assert len(lines) == 1 and "n=1" in lines[0] and "input=10" in lines[0]
    assert "2026-09-24" not in lines[0] and "ATO" not in lines[0]
    assert any("calls=2" in r.getMessage() for r in caplog.records)


def test_default_is_one_model_call_per_round_even_if_paused(api, client, payload, settings):
    assert settings.max_pauses == 0
    client.messages.queue("good", stop="pause_turn")      # files were saved, then the model paused
    b = post(api, payload).json()
    assert b["status"] == "ok" and len(client.messages.calls) == 1


def test_cut_off_reply_fails_clearly_not_with_a_400(api, client, payload):
    client.messages.queue(None, stop="max_tokens")
    r = post(api, payload)
    assert r.status_code == 502 and "max_tokens" in r.json()["detail"]
    assert len(client.messages.calls) == 1                 # no repair call after a truncated reply
