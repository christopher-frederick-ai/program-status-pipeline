from pathlib import Path

import pytest

from app.agents import AgentRegistry, load_instructions
from app.runner import extract_json

from .conftest import make_analyst_docx


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go:\n{"a": 1}\nHope that helps') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_docx_loader_keeps_order_and_tables(tmp_path):
    p = tmp_path / "i.docx"
    make_analyst_docx(p)
    text = load_instructions(p)
    assert text.index("experienced") < text.index("field | meaning") < text.index("Return JSON only.")


def test_empty_and_unsupported_files(tmp_path):
    (tmp_path / "e.md").write_text("   ")
    with pytest.raises(ValueError):
        load_instructions(tmp_path / "e.md")
    (tmp_path / "x.pdf").write_text("x")
    with pytest.raises(ValueError):
        load_instructions(tmp_path / "x.pdf")


def test_corrupt_docx_marks_agent_unavailable_not_crash(tmp_path):
    from dataclasses import replace

    from app.agents import AGENTS

    # The shipped Analyst prompt is a .md now, but .docx instructions stay supported.
    specs = [
        replace(s, instructions_file="bad.docx") if s.name == "milestone_risk_analyst" else s
        for s in AGENTS
    ]
    (tmp_path / "bad.docx").write_text("not a real docx")
    (tmp_path / "milestone_risk_executor.md").write_text("ok")
    reg = AgentRegistry(tmp_path, specs)
    assert not reg.get("milestone_risk_analyst").available
    assert reg.get("milestone_risk_executor").available


def test_shipped_executor_instructions_load_and_state_key_rule():
    real = Path(__file__).resolve().parent.parent / "agents"
    text = load_instructions(real / "milestone_risk_executor.md")
    assert "Lead the paragraph with the substance of the `status` field" in text
    assert "Do NOT foreground `milestone_status_flagged`" in text
    assert "Never use semicolons" in text
    assert "resource conflict" in text
    assert "calls minor" in text


def test_shipped_slide_title_instructions_load_and_state_key_rule():
    real = Path(__file__).resolve().parent.parent / "agents"
    text = load_instructions(real / "milestone_slide_title.md")
    assert "3 to 6 words" in text
    assert "not restate the title" in text.lower() or "not a restatement" in text.lower()
    assert "Back On Track" in text  # the "reflect improvement, don't default to stale wording" rule


def test_shipped_analyst_instructions_load_cleanly():
    real = Path(__file__).resolve().parent.parent / "agents"
    from app.schemas import MilestoneRiskAnalystOutput

    text = load_instructions(real / "milestone_risk_analyst.md")
    assert text.startswith("# Milestone & Risk Analyst")
    assert "Closed" in text
    # Rules added while tuning with scripts/repeat_runs.py:
    assert "resource conflict" in text and "comfortable" in text
    assert "separate questions" in text and "overdue" in text
    # The prompt must name every key the output schema requires.
    for key in MilestoneRiskAnalystOutput.model_fields:
        assert f"`{key}`" in text


def test_analyst_schema_accepts_title_case_keys_and_keeps_extras():
    from app.schemas import MilestoneRiskAnalystOutput

    out = MilestoneRiskAnalystOutput.model_validate(
        {
            "Confidence score": 4,
            "Milestone status flagged": "Yes",
            "Status": "At risk.",
            "Explanation": "Because.",
            "Closed risks recommended for removal": ["RISK-021"],
        }
    ).model_dump()
    assert out["milestone_status_flagged"] == "yes"
    assert out["closed_risks_recommended_for_removal"] == ["RISK-021"]  # not silently dropped
    assert out["risks_recommended_for_removal"] == []


def test_error_summary_names_fields_without_content():
    import json as _json

    from pydantic import ValidationError

    from app.runner import _error_summary
    from app.schemas import MilestoneRiskAnalystOutput

    try:
        MilestoneRiskAnalystOutput.model_validate({"confidence_score": 9, "status": "SECRET TEXT"})
    except ValidationError as exc:
        msg = _error_summary(exc)
    assert "confidence_score" in msg and "milestone_status_flagged: missing" in msg
    assert "SECRET TEXT" not in msg
    try:
        _json.loads("{not json")
    except _json.JSONDecodeError as exc:
        assert _error_summary(exc).startswith("JSON parse error at char")


def test_proposed_prompts_are_the_live_prompts_plus_the_tuning_rules():
    """agents/proposed/ holds the next prompt versions. Skipped once they are promoted and the folder is deleted."""
    agents = Path(__file__).resolve().parent.parent / "agents"
    if not (agents / "proposed").is_dir():
        pytest.skip("no proposed prompts")
    from app.schemas import MilestoneRiskAnalystOutput

    analyst = load_instructions(agents / "proposed" / "milestone_risk_analyst.md")
    executor = load_instructions(agents / "proposed" / "milestone_risk_executor.md")
    assert "resource conflict" in analyst and "comfortable" in analyst and "separate questions" in analyst
    assert "resource conflict" in executor and "Never use semicolons" in executor
    for key in MilestoneRiskAnalystOutput.model_fields:
        assert f"`{key}`" in analyst
