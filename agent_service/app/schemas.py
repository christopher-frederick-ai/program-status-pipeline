"""Request/response models and per-agent output schemas."""

from datetime import date
from typing import Any, Literal

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    """Body for POST /agents/{name}/run.

    `input` may be a string (sent to the agent as-is) or any JSON value (dict/list),
    which is serialized to indented JSON before being sent to the agent.
    """

    input: Any = Field(description="The data the agent should work on.")


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class RunResponse(BaseModel):
    agent: str
    request_id: str
    output: Any  # dict for JSON agents, str for text agents
    model: str
    usage: Usage  # summed across attempts
    attempts: int
    latency_ms: int


class AgentInfo(BaseModel):
    name: str
    description: str
    output_kind: Literal["json", "text"]
    instructions_file: str
    available: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Agent output schemas
# ---------------------------------------------------------------------------
class MilestoneRiskAnalystOutput(BaseModel):
    """Output contract of the Milestone & Risk Analyst (agents/milestone_risk_analyst.md)."""

    # Keep unrecognized fields rather than dropping them, so a renamed or extra field from
    # the model stays visible instead of silently vanishing. The prompt names the keys.
    model_config = ConfigDict(extra="allow")

    confidence_score: int = Field(ge=1, le=5)
    milestone_status_flagged: Literal["yes", "no"]
    status: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    # Closed-status risks tied to the milestone. Kept permissive (strings or objects).
    risks_recommended_for_removal: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_keys(cls, data: Any) -> Any:
        """Accept "Confidence score", "confidence-score", etc. as confidence_score."""
        if not isinstance(data, dict):
            return data
        return {re.sub(r"[\s\-]+", "_", str(k).strip().lower()): v for k, v in data.items()}

    @field_validator("milestone_status_flagged", mode="before")
    @classmethod
    def _normalize_flag(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, str):
            return v.strip().lower()
        return v


class MilestoneSlideTitleOutput(BaseModel):
    """Output contract of the Milestone Slide Title agent (agents/milestone_slide_title.md).

    Pure compression of an already-approved paragraph into a short slide title and a
    one-line status subtitle. No new judgment; a small, bounded field, unlike the Analyst's
    free-form prose, so wording variance here isn't the risk that it is elsewhere.
    """

    model_config = ConfigDict(extra="allow")

    title: str = Field(min_length=1, max_length=80)
    subtitle: str = Field(min_length=1, max_length=80)

    @model_validator(mode="before")
    @classmethod
    def _normalize_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {re.sub(r"[\s\-]+", "_", str(k).strip().lower()): v for k, v in data.items()}


# ---------------------------------------------------------------------------
# POST /clusters
# ---------------------------------------------------------------------------
class ClustersRequest(BaseModel):
    """Raw rows as n8n has read them; the "Related Milestone" join is done by the service.

    Rows are keyed like the source exports: milestones by Milestone / Planned Date /
    Forecast/Actual Date / Status; tasks by Task Name / Notes / Assignee / Due Date /
    Section/Column / Related Milestone; risks the same plus Likelihood / Impact.
    """

    milestones: list[dict[str, Any]] = Field(min_length=1, description="Milestone reference rows.")
    tasks: list[dict[str, Any]] = Field(default_factory=list, description="Task tracker rows.")
    risks: list[dict[str, Any]] = Field(default_factory=list, description="Risk registrar rows.")
    engineering_notes: str = Field(default="", description="The full engineering notes text.")
    as_of_date: date = Field(description="Report date, YYYY-MM-DD (in n8n: {{ $today.toISODate() }}).")
    include_empty: bool = Field(
        default=False, description="Also return milestones that have no linked tasks or risks."
    )


class ClustersResponse(BaseModel):
    request_id: str
    as_of_date: str
    clusters: list[dict[str, Any]]  # one per milestone, ready to send to the Analyst as `input`
    skipped: list[dict[str, str]]  # milestones left out, with the reason
    warnings: list[str]  # e.g. rows tagged to a milestone that does not exist
