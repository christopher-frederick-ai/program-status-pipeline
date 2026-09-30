import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

from .conftest import ANALYST_JSON, HEADERS, SECRET, j


def run(client, agent, payload, headers=HEADERS):
    return client.post(f"/agents/{agent}/run", json={"input": payload}, headers=headers)


# --- health & auth ---------------------------------------------------------
def test_health_needs_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["agents_available"] == 3


def test_agents_requires_token(client):
    assert client.get("/agents").status_code == 401
    assert client.get("/agents", headers={"X-Service-Token": "wrong"}).status_code == 401
    r = client.get("/agents", headers=HEADERS)
    assert r.status_code == 200
    assert {a["name"] for a in r.json()} == {
        "milestone_risk_analyst",
        "milestone_risk_executor",
        "milestone_slide_title",
    }


def test_run_requires_token(client):
    assert run(client, "milestone_risk_analyst", "x", headers={}).status_code == 401
    assert client.fake.calls == []  # LLM never touched


def test_startup_fails_without_secret(monkeypatch, agents_dir):
    monkeypatch.setenv("SERVICE_SHARED_SECRET", "")
    monkeypatch.setenv("AGENTS_DIR", str(agents_dir))
    get_settings.cache_clear()
    from app.main import app

    with pytest.raises(RuntimeError, match="SERVICE_SHARED_SECRET"):
        with TestClient(app):
            pass
    get_settings.cache_clear()


# --- analyst (JSON agent) --------------------------------------------------
def test_analyst_success_normalizes_and_validates(client):
    reply = "```json\n" + j({**ANALYST_JSON, "milestone_status_flagged": "Yes"}) + "\n```"
    client.fake.replies = [reply]
    r = run(client, "milestone_risk_analyst", {"milestone": "CDR", "tasks": []})
    assert r.status_code == 200
    body = r.json()
    assert body["output"]["milestone_status_flagged"] == "yes"
    assert body["attempts"] == 1
    assert body["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert r.headers["X-Request-ID"] == body["request_id"]
    # dict input was serialized to JSON text for the model; instructions became the system prompt
    call = client.fake.calls[0]
    assert call["messages"][0]["content"].startswith("Source data:\n{")  # tested-harness prefix
    assert '"milestone": "CDR"' in call["messages"][0]["content"]
    assert "experienced technical program manager" in call["system"]
    assert "Return JSON only." in call["system"]


def test_analyst_retries_once_on_bad_json(client):
    client.fake.replies = ["Sure! Here is my analysis...", j(ANALYST_JSON)]
    r = run(client, "milestone_risk_analyst", "cluster text")
    assert r.status_code == 200
    body = r.json()
    assert body["attempts"] == 2
    assert body["usage"]["input_tokens"] == 20  # summed across attempts
    retry_msgs = client.fake.calls[1]["messages"]
    assert [m["role"] for m in retry_msgs] == ["user", "assistant", "user"]
    assert "confidence_score, milestone_status_flagged, status, explanation" in retry_msgs[2]["content"]


def test_analyst_retries_on_schema_violation(client):
    bad = {**ANALYST_JSON, "confidence_score": 9}
    client.fake.replies = [j(bad), j(ANALYST_JSON)]
    r = run(client, "milestone_risk_analyst", "cluster text")
    assert r.status_code == 200 and r.json()["attempts"] == 2


def test_analyst_gives_502_after_two_bad_replies(client):
    client.fake.replies = ["nope", "still nope"]
    r = run(client, "milestone_risk_analyst", "cluster text")
    assert r.status_code == 502
    assert len(client.fake.calls) == 2


# --- slide title (JSON agent, small bounded field) --------------------------
def test_slide_title_returns_title_and_subtitle(client):
    client.fake.replies = [j({"title": "ATO Approval Delayed", "subtitle": "Behind schedule"})]
    r = run(client, "milestone_slide_title", {"milestone": "ATO", "paragraph": "ATO is behind."})
    assert r.status_code == 200
    body = r.json()
    assert body["output"] == {"title": "ATO Approval Delayed", "subtitle": "Behind schedule"}
    assert client.fake.calls[0]["max_tokens"] == 600


def test_slide_title_accepts_capitalized_keys(client):
    client.fake.replies = [j({"Title": "CDR On Track", "Subtitle": "On track"})]
    r = run(client, "milestone_slide_title", "x")
    assert r.status_code == 200
    assert r.json()["output"] == {"title": "CDR On Track", "subtitle": "On track"}


def test_slide_title_rejects_a_title_that_is_just_too_long(client):
    # min/max_length catches a reply that ignored the "3 to 6 words" instruction outright,
    # e.g. the model echoing back a full paragraph instead of compressing it.
    bad = {"title": "x" * 81, "subtitle": "ok"}
    client.fake.replies = [j(bad), j({"title": "ATO Approval Delayed", "subtitle": "Behind schedule"})]
    r = run(client, "milestone_slide_title", "x")
    assert r.status_code == 200 and r.json()["attempts"] == 2


# --- executor (text agent) -------------------------------------------------
def test_executor_returns_text(client):
    client.fake.replies = ["  CDR is at risk. The reference needs updating.  "]
    r = run(client, "milestone_risk_executor", {"milestone": "CDR", **ANALYST_JSON})
    assert r.status_code == 200
    assert r.json()["output"] == "CDR is at risk. The reference needs updating."
    assert client.fake.calls[0]["max_tokens"] == 2000


def test_text_agent_does_not_retry_a_good_reply(client):
    client.fake.replies = ["A paragraph."]
    r = run(client, "milestone_risk_executor", "x")
    assert r.status_code == 200 and r.json()["attempts"] == 1
    assert len(client.fake.calls) == 1


# --- error handling --------------------------------------------------------
def test_unknown_agent_404(client):
    assert run(client, "planner", "x").status_code == 404


def test_missing_instructions_file_gives_503_but_service_runs(client, agents_dir):
    (agents_dir / "milestone_risk_analyst.md").unlink()
    assert client.post("/admin/reload", headers=HEADERS).status_code == 200
    r = run(client, "milestone_risk_analyst", "x")
    assert r.status_code == 503
    assert r.json()["detail"] == "Instructions file not found: milestone_risk_analyst.md"
    assert client.get("/health").json()["agents_available"] == 2
    # the other agents still work
    client.fake.replies = ["ok text"]
    assert run(client, "milestone_risk_executor", "x").status_code == 200


def test_reload_picks_up_new_file(client, agents_dir):
    (agents_dir / "milestone_risk_executor.md").write_text("NEW INSTRUCTIONS")
    client.post("/admin/reload", headers=HEADERS)
    client.fake.replies = ["out"]
    run(client, "milestone_risk_executor", "x")
    assert client.fake.calls[0]["system"] == "NEW INSTRUCTIONS"


@pytest.mark.parametrize("bad", ["", {}, [], None])
def test_empty_input_422(client, bad):
    assert run(client, "milestone_risk_executor", bad).status_code == 422


def test_oversize_input_413(client, monkeypatch):
    monkeypatch.setenv("MAX_INPUT_CHARS", "50")
    get_settings.cache_clear()
    assert run(client, "milestone_risk_executor", "x" * 51).status_code == 413


def test_upstream_timeout_maps_to_504(client):
    from app.llm import LLMError

    client.fake.replies = [LLMError("The LLM request timed out.", 504)]
    assert run(client, "milestone_risk_executor", "x").status_code == 504


# --- empty replies from the model -------------------------------------------------
def test_text_agent_retries_once_when_the_model_returns_nothing(client, caplog):
    client.fake.replies = ["  ", "The paragraph."]
    with caplog.at_level("WARNING"):
        r = run(client, "milestone_risk_executor", "x")
    assert r.status_code == 200
    body = r.json()
    assert body["output"] == "The paragraph." and body["attempts"] == 2
    assert body["usage"]["input_tokens"] == 20  # summed across both attempts
    first, second = client.fake.calls
    assert first["messages"] == second["messages"]  # nothing to echo back, so the same request is retried
    warning = next(rec.getMessage() for rec in caplog.records if "empty response" in rec.getMessage())
    assert "stop_reason=end_turn" in warning and "out_tokens=5" in warning
    assert "milestone_risk_executor" in warning


def test_text_agent_gives_502_after_two_empty_replies(client):
    client.fake.replies = ["", ""]
    r = run(client, "milestone_risk_executor", "x")
    assert r.status_code == 502
    assert "empty response" in r.json()["detail"]
    assert len(client.fake.calls) == 2


def test_text_agent_cut_off_at_max_tokens_gets_a_specific_message(client, caplog):
    # Seen live: the model can burn the whole max_tokens budget before any visible text,
    # which is a different, fixable problem (too small a cap) from a genuinely empty reply.
    client.fake.replies = [("", "max_tokens"), ("", "max_tokens")]
    with caplog.at_level("WARNING"):
        r = run(client, "milestone_risk_executor", "x")
    assert r.status_code == 502
    assert "cut off at max_tokens" in r.json()["detail"]
    warning = next(rec.getMessage() for rec in caplog.records if "empty response" in rec.getMessage())
    assert "stop_reason=max_tokens" in warning and "raise this agent's max_tokens" in warning
